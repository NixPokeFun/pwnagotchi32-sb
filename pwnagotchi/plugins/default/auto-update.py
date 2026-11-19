import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import time
import zipfile
from contextlib import contextmanager
from threading import Lock

import requests

import pwnagotchi
import pwnagotchi.plugins as plugins
from pwnagotchi.utils import StatusFile

API_URL = "https://api.github.com/repos/%s/releases/latest"
HTTP_TIMEOUT = 20
CHECKSUM_EXT = ('.sha256', '.sha256sum', '.sha256sums')

# Default repositories. Can be overridden in config.toml:
# main.plugins.auto-update.repos = [...]  (see DEFAULT_REPOS for the format)
DEFAULT_REPOS = [
    {'repo': 'NixPokeFun/bettercap', 'name': 'bettercap', 'service': 'bettercap',
     'version_cmd': 'bettercap -version', 'dest': '/usr/local/bin/bettercap'},
    {'repo': 'jayofelony/pwngrid', 'name': 'pwngrid', 'service': 'pwngrid-peer',
     'version_cmd': 'pwngrid -version', 'dest': '/usr/local/bin/pwngrid'},
]


# ---------------------------------------------------------------- helpers ---

def ver_tuple(version):
    """'v2.32.0-rc1' -> (2, 32, 0). Tolerates suffixes."""
    core = re.split(r'[-+]', str(version).lstrip('vV'))[0]
    return tuple(int(x) for x in re.findall(r'\d+', core))


def local_version(cmd):
    out = subprocess.getoutput(cmd)
    m = re.search(r'(?<![\w.])v?(\d+\.\d+\.\d+[\w.+-]*)', out)
    if not m:
        raise RuntimeError('could not parse version from "%s": %s' % (cmd, out))
    return m.group(1)


def asset_version(asset_name, fallback):
    """Version from the asset name (bettercap-2.32.2-armhf.zip -> 2.32.2).
    In the fork the release tag (v1.0.1) does not match the binary version, so the tag is only a fallback."""
    m = re.search(r'(?<![\d.])(\d+\.\d+\.\d+)(?![\d])', asset_name)
    return m.group(1) if m else fallback


def arch_keywords():
    m = platform.machine().lower()
    if m.startswith('aarch') or m == 'arm64':
        return ['aarch64', 'arm64', 'aarch']
    if m.startswith('arm'):
        return [m, 'armhf', 'arm']
    return [m]


def arch_match(asset_name, keys):
    n = asset_name.lower()
    wants_64 = any(k in ('aarch64', 'arm64', 'aarch') for k in keys)
    if not wants_64 and ('arm64' in n or 'aarch' in n):
        return False
    return any(k in n for k in keys)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fp:
        for chunk in iter(lambda: fp.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=60)


def download(url, dest):
    # No token here: it is not needed for asset downloads and is dropped on redirects anyway
    with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        with open(dest, 'wb') as fp:
            for chunk in r.iter_content(65536):
                fp.write(chunk)


def parse_expected(text, names):
    """Extracts a sha256 from a checksum file in any common format:
    'hash  file', 'SHA256(file)= hash', a bare hash, or a multi-file sha256sums list."""
    lines = [l for l in text.splitlines() if l.strip()]
    hashes = set()
    for line in lines:
        m = re.search(r'\b([a-fA-F0-9]{64})\b', line)
        if not m:
            continue
        h = m.group(1).lower()
        hashes.add(h)
        if any(n and n in line for n in names):
            return h
    return hashes.pop() if len(hashes) == 1 else None


# ------------------------------------------------------------ core logic ---

def check(session, entry, current):
    repo = entry['repo']
    logging.debug("[update] checking %s, local is %s" % (repo, current))

    resp = session.get(API_URL % repo, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError("GitHub API returned %s for %s" % (resp.status_code, repo))
    logging.debug("[update] GitHub requests remaining: %s" % resp.headers.get('X-RateLimit-Remaining'))

    latest = resp.json()
    tag = latest['tag_name']
    info = {
        'repo': repo, 'name': entry['name'], 'service': entry['service'],
        'dest': entry.get('dest'),
        'current': current, 'available': tag.lstrip('vV'),
        'binary': None, 'checksum': None,
    }

    keys = arch_keywords()
    assets = latest.get('assets', [])
    sums = [a for a in assets if a['name'].lower().endswith(CHECKSUM_EXT)]
    bins = [a for a in assets if a not in sums and arch_match(a['name'], keys)
            and (a['name'].lower().endswith('.zip') or '.' not in a['name'])]
    if not bins:
        logging.warning("[update] %s %s: no asset for arch %s" % (repo, tag, platform.machine()))
        return info

    info['available'] = asset_version(bins[0]['name'], info['available'])
    if ver_tuple(info['available']) <= ver_tuple(current):
        return info

    info['binary'] = bins[0]
    stem = bins[0]['name'].lower()
    stem = stem[:-4] if stem.endswith('.zip') else stem
    for s in sums:
        if s['name'].lower().startswith(stem):
            info['checksum'] = s
            break
    else:
        matching = [s for s in sums if arch_match(s['name'], keys)]
        if len(matching) == 1:
            info['checksum'] = matching[0]
        elif len(sums) == 1:
            info['checksum'] = sums[0]
    return info


def prepare(info, work_root, display):
    """Downloads, extracts and verifies. Returns the path to the ready binary."""
    name = info['name']
    path = os.path.join(work_root, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)

    asset = info['binary']
    asset_path = os.path.join(path, asset['name'])
    display.update(force=True, new_data={'status': 'Downloading %s %s ...' % (name, info['available'])})
    logging.info("[update] downloading %s" % asset['browser_download_url'])
    download(asset['browser_download_url'], asset_path)

    binary_path = os.path.join(path, name)
    sum_texts = []

    if asset['name'].lower().endswith('.zip'):
        display.update(force=True, new_data={'status': 'Extracting %s %s ...' % (name, info['available'])})
        found = False
        with zipfile.ZipFile(asset_path) as z:
            for member in z.infolist():
                base = os.path.basename(member.filename)
                if member.is_dir() or not base:
                    continue
                # Extract only the needed members, by basename only -> no zip-slip
                if base == name:
                    with z.open(member) as src, open(binary_path, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    found = True
                elif base.lower().endswith(CHECKSUM_EXT):
                    sum_texts.append(z.read(member).decode('utf-8', 'ignore'))
        if not found:
            raise RuntimeError("binary '%s' not found inside %s" % (name, asset['name']))
    else:
        shutil.move(asset_path, binary_path)
        asset_path = binary_path

    if info['checksum']:
        sum_path = os.path.join(path, info['checksum']['name'])
        download(info['checksum']['browser_download_url'], sum_path)
        with open(sum_path, 'rt', errors='ignore') as fp:
            sum_texts.append(fp.read())

    display.update(force=True, new_data={'status': 'Verifying %s %s ...' % (name, info['available'])})
    if not sum_texts:
        raise RuntimeError("no SHA256 checksum for %s, refusing to install" % name)

    # The checksum may cover either the archive or the extracted binary
    real = {sha256_of(binary_path), sha256_of(asset_path)}
    names = [asset['name'], name]
    expected = [parse_expected(t, names) for t in sum_texts]
    if not any(e in real for e in expected if e):
        raise RuntimeError("checksum mismatch for %s: expected=%s" % (name, expected))

    with open(binary_path, 'rb') as fp:
        if fp.read(4) != b'\x7fELF':
            raise RuntimeError("%s is not an ELF binary" % name)

    return binary_path


@contextmanager
def maintenance_mode(agent):
    """While a service is being swapped: tell pwnagotchi that bettercap downtime is expected
    (keeps the log free of reconnect warnings) and pause the AI. Every hook is optional,
    so this is a no-op on builds that do not provide them."""
    bc = None
    try:
        import pwnagotchi.bettercap as bc
        if not hasattr(bc, 'EXPECTED_DOWNTIME'):
            bc = None
    except Exception:
        bc = None

    paused_by_us = False
    try:
        if all(hasattr(agent, m) for m in ('is_ai_paused', 'pause_ai', 'resume_ai')):
            if not agent.is_ai_paused():
                agent.pause_ai()
                paused_by_us = True
    except Exception as e:
        logging.warning("[update] couldn't pause AI: %s" % e)

    if bc is not None:
        bc.EXPECTED_DOWNTIME = True
    try:
        yield
    finally:
        # give the bettercap API a moment to come up before warnings count again
        time.sleep(5)
        if bc is not None:
            bc.EXPECTED_DOWNTIME = False
        if paused_by_us:
            try:
                agent.resume_ai()
            except Exception as e:
                logging.warning("[update] couldn't resume AI: %s" % e)


def replace_binary(src, dest, service):
    """Stop the service, swap the binary, start it again. Roll back on failure."""
    backup = dest + '.bak'
    tmp = dest + '.new'
    shutil.copy2(dest, backup)
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o755)

    logging.info("[update] stopping %s ..." % service)
    run(['service', service, 'stop'])
    try:
        os.replace(tmp, dest)
        logging.info("[update] starting %s ..." % service)
        run(['service', service, 'start'])
        time.sleep(3)
        if run(['service', service, 'status']).returncode != 0:
            raise RuntimeError("service %s is not running after update" % service)
    except Exception:
        logging.error("[update] rolling back %s" % dest)
        os.replace(backup, dest)
        run(['service', service, 'start'])
        raise


def install(agent, display, info, work_root):
    name = info['name']
    dest = info.get('dest') or shutil.which(name)
    if not dest:
        raise RuntimeError("can't find path for %s" % name)

    new_binary = prepare(info, work_root, display)
    display.update(force=True, new_data={'status': 'Installing %s %s ...' % (name, info['available'])})
    with maintenance_mode(agent):
        replace_binary(new_binary, dest, info['service'])
    shutil.rmtree(os.path.join(work_root, name), ignore_errors=True)

    display.update(force=True, new_data={'status': '%s updated to %s!' % (name, info['available'])})
    time.sleep(3)


# ----------------------------------------------------------------- plugin ---

class AutoUpdate(plugins.Plugin):
    __author__ = 'evilsocket@gmail.com, flippedgannet'
    __version__ = '1.0.0'
    __name__ = 'auto-update'
    __license__ = 'GPL3'
    __description__ = 'Checks for bettercap/pwngrid updates and installs them (checksum + rollback).'

    def __init__(self):
        self.ready = False
        self.status = StatusFile('/root/.auto-update')
        self.lock = Lock()
        self.options = dict()

    def on_loaded(self):
        if not self.options.get('interval'):
            logging.error("[update] main.plugins.auto-update.interval is not set")
            return
        self.ready = True
        logging.info("[update] plugin loaded.")

    def on_internet_available(self, agent):
        if self.lock.locked() or not self.ready:
            return

        with self.lock:
            interval = self.options['interval']
            if self.status.newer_then_hours(interval):
                logging.debug("[update] last check happened less than %d hours ago" % interval)
                return

            logging.info("[update] checking for updates ...")
            display = agent.view()
            prev_status = display.get('status')

            session = requests.Session()
            token = self.options.get('token')
            if token:
                session.headers['Authorization'] = 'token %s' % token

            work_root = self.options.get('work_dir', '/opt/auto-update')
            do_install = bool(self.options.get('install', False))
            repos = self.options.get('repos') or DEFAULT_REPOS

            try:
                display.update(force=True, new_data={'status': 'Checking for updates ...'})

                to_install = []
                for entry in repos:
                    try:
                        info = check(session, entry, local_version(entry['version_cmd']))
                    except Exception as e:
                        logging.error("[update] %s: %s" % (entry.get('repo'), e))
                        continue
                    if info['binary'] is not None:
                        logging.warning("[update] %s: %s -> %s" % (
                            info['repo'], info['current'], info['available']))
                        to_install.append(info)

                installed = 0
                if to_install:
                    if do_install:
                        for info in to_install:
                            plugins.on('updating')
                            try:
                                install(agent, display, info, work_root)
                                installed += 1
                            except Exception as e:
                                logging.error("[update] failed to update %s: %s" % (info['name'], e))
                    else:
                        prev_status = '%d new update%s available!' % (
                            len(to_install), 's' if len(to_install) > 1 else '')

                logging.info("[update] done")
                self.status.update()

                if installed and self.options.get('reboot', False):
                    display.update(force=True, new_data={'status': 'Rebooting ...'})
                    pwnagotchi.reboot()

            except Exception as e:
                logging.error("[update] %s" % e)

            display.update(force=True, new_data={'status': prev_status if prev_status is not None else ''})
