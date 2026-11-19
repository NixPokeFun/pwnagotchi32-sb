import hashlib
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
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
PKG_DEFAULT_REPO = 'NixPokeFun/pwnagotchi32-sb'
PKG_DEFAULT_BRANCH = 'main'
PKG_STATE_FILE = '/root/.auto-update-pkg'
PKG_BIN_STATE_FILE = '/root/.auto-update-pkg-bin'
PKG_BIN_DIR = '/usr/local/bin'
PKG_BINARIES = ('pwnagotchi', 'pwnygen')
PKG_SERVICE = 'pwnagotchi'
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


def run(cmd, timeout=60, cwd=None):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=timeout, cwd=cwd)


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


# --------------------------------------------------- pwnagotchi package ---

def read_state(path):
    try:
        with open(path, 'rt') as fp:
            return fp.read().strip() or None
    except FileNotFoundError:
        return None


def write_state(path, value):
    tmp = path + '.tmp'
    with open(tmp, 'wt') as fp:
        fp.write(value)
    os.replace(tmp, path)


def resolve_package_dir(option):
    """Where the live pwnagotchi package is installed. Refuses anything that does not look
    like an installed package (for example a git checkout) unless the path is set explicitly."""
    path = os.path.realpath(option or os.path.dirname(os.path.abspath(pwnagotchi.__file__)))
    if os.path.basename(path) != 'pwnagotchi':
        raise RuntimeError("unexpected package directory: %s" % path)
    if not option and not re.search(r'(dist|site)-packages', path):
        raise RuntimeError("pwnagotchi is not running from dist-packages/site-packages (%s); "
                           "set pwnagotchi_dir explicitly to allow updating it" % path)
    return path


def check_package(session, repo, branch):
    """Checks package and bundled binary revisions independently."""
    resp = session.get("https://api.github.com/repos/%s/commits/%s" % (repo, branch),
                       timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError("GitHub API returned %s for %s@%s" % (resp.status_code, repo, branch))
    data = resp.json()
    sha = data['sha']
    installed = read_state(PKG_STATE_FILE)
    binaries_installed = read_state(PKG_BIN_STATE_FILE)
    update_package = installed != sha
    update_binaries = binaries_installed != sha
    if not update_package and not update_binaries:
        return None
    return {
        'repo': repo, 'branch': branch, 'sha': sha, 'label': sha[:7],
        'installed': installed[:7] if installed else None,
        'binaries_installed': binaries_installed[:7] if binaries_installed else None,
        'update_package': update_package,
        'update_binaries': update_binaries,
        'message': data['commit']['message'].split('\n')[0],
        # Pin the archive to the exact commit that was checked, not to the moving branch.
        'url': "https://github.com/%s/archive/%s.zip" % (repo, sha),
    }


def extract_package_dir(zip_path, dest):
    """Extracts only '<root>/pwnagotchi/**' from a GitHub source archive into dest."""
    count = 0
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            parts = member.filename.split('/')
            if len(parts) < 3 or parts[1] != 'pwnagotchi' or member.is_dir():
                continue
            rel = os.path.normpath(os.path.join(*parts[2:]))
            if os.path.isabs(rel) or rel.split(os.sep)[0] == '..':
                raise RuntimeError("unsafe path in archive: %s" % member.filename)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                continue
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            if mode & 0o111:
                os.chmod(target, 0o755)
            count += 1
    if count == 0 or not os.path.isfile(os.path.join(dest, '__init__.py')):
        raise RuntimeError("no valid pwnagotchi/ package found in the archive")


def validate_executable(path, name):
    """Reject empty or unrecognized files before placing them in the executable path."""
    if os.path.getsize(path) == 0:
        raise RuntimeError("%s is empty" % name)
    with open(path, 'rb') as fp:
        header = fp.read(4)
    if header != b'\x7fELF' and not header.startswith(b'#!'):
        raise RuntimeError("%s is not a supported Linux executable" % name)


def extract_package_binaries(zip_path, dest):
    """Extracts the two maintained executables from the repository's top-level bin/ folder."""
    os.makedirs(dest, exist_ok=True)
    found = set()
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            parts = member.filename.replace('\\', '/').split('/')
            if member.is_dir() or len(parts) < 3 or parts[-2] != 'bin':
                continue
            name = parts[-1]
            if name not in PKG_BINARIES:
                continue
            if name in found:
                raise RuntimeError("duplicate %s entry in the source archive" % name)
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError("refusing symlink for bundled executable: %s" % name)
            target = os.path.join(dest, name)
            with z.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            validate_executable(target, name)
            os.chmod(target, 0o755)
            found.add(name)

    missing = sorted(set(PKG_BINARIES) - found)
    if missing:
        raise RuntimeError("source archive is missing bin/%s" % ', bin/'.join(missing))


def swap_package_binaries(new_dir, live_dir):
    """Atomically installs both bundled executables and rolls back the pair on failure."""
    os.makedirs(live_dir, exist_ok=True)
    completed = []
    temporary_paths = []
    try:
        for name in PKG_BINARIES:
            src = os.path.join(new_dir, name)
            dest = os.path.join(live_dir, name)
            tmp = dest + '.new'
            backup = dest + '.bak'
            temporary_paths.append(tmp)

            if os.path.lexists(tmp):
                os.unlink(tmp)
            if os.path.lexists(backup):
                os.unlink(backup)

            shutil.copy2(src, tmp)
            os.chmod(tmp, 0o755)
            validate_executable(tmp, name)

            had_old = os.path.lexists(dest)
            if had_old:
                shutil.copy2(dest, backup)
            os.replace(tmp, dest)
            completed.append((dest, backup, had_old))
    except Exception:
        for dest, backup, had_old in reversed(completed):
            try:
                if had_old:
                    os.replace(backup, dest)
                elif os.path.lexists(dest):
                    os.unlink(dest)
            except Exception as rollback_error:
                logging.critical("[update] failed to roll back %s: %s", dest, rollback_error)
        for tmp in temporary_paths:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        raise


def swap_package(new_dir, live_dir):
    """Replace live_dir with new_dir via same-filesystem renames. The previous package is kept as
    '.pwnagotchi.bak' next to it. Rolls back if the new package fails to import."""
    parent = os.path.dirname(live_dir)
    staging = os.path.join(parent, '.pwnagotchi.new')
    backup = os.path.join(parent, '.pwnagotchi.bak')
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)

    shutil.copytree(new_dir, staging)
    swapped = False
    try:
        os.rename(live_dir, backup)
        swapped = True
        os.rename(staging, live_dir)
        # cwd=parent makes "import pwnagotchi" resolve to exactly the directory we just swapped in
        res = run([sys.executable, '-c', 'import pwnagotchi'], timeout=120, cwd=parent)
        if res.returncode != 0:
            raise RuntimeError("new package fails to import: %s" % res.stdout.strip()[-500:])
    except Exception:
        logging.error("[update] rolling back %s" % live_dir)
        if swapped:
            shutil.rmtree(live_dir, ignore_errors=True)
            os.rename(backup, live_dir)
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install_package(display, info, work_root, live_dir=None, bin_dir=PKG_BIN_DIR):
    """Installs requested repository components and records each successful revision."""
    label = info['label']
    path = os.path.join(work_root, 'pwnagotchi-source')
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)

    package_installed = False
    binaries_installed = False
    try:
        zip_path = os.path.join(path, 'source.zip')
        display.update(force=True, new_data={'status': 'Downloading pwnagotchi %s ...' % label})
        logging.info("[update] downloading %s" % info['url'])
        download(info['url'], zip_path)

        if info.get('update_package'):
            try:
                display.update(force=True, new_data={'status': 'Extracting pwnagotchi %s ...' % label})
                new_dir = os.path.join(path, 'pwnagotchi')
                extract_package_dir(zip_path, new_dir)

                display.update(force=True, new_data={'status': 'Verifying pwnagotchi %s ...' % label})
                res = run([sys.executable, '-m', 'compileall', '-q', new_dir], timeout=300)
                if res.returncode != 0:
                    raise RuntimeError("syntax check failed: %s" % res.stdout.strip()[-500:])

                display.update(force=True, new_data={'status': 'Installing pwnagotchi %s ...' % label})
                swap_package(new_dir, live_dir)
                write_state(PKG_STATE_FILE, info['sha'])
                package_installed = True
            except Exception as e:
                logging.error("[update] pwnagotchi package update failed: %s" % e)

        if info.get('update_binaries'):
            try:
                display.update(force=True, new_data={'status': 'Extracting pwnagotchi binaries %s ...' % label})
                new_bin_dir = os.path.join(path, 'bin')
                extract_package_binaries(zip_path, new_bin_dir)

                display.update(force=True, new_data={'status': 'Verifying pwnagotchi binaries %s ...' % label})
                for name in PKG_BINARIES:
                    validate_executable(os.path.join(new_bin_dir, name), name)

                display.update(force=True, new_data={'status': 'Installing pwnagotchi binaries %s ...' % label})
                swap_package_binaries(new_bin_dir, bin_dir)
                write_state(PKG_BIN_STATE_FILE, info['sha'])
                binaries_installed = True
            except Exception as e:
                logging.error("[update] pwnagotchi binary update failed: %s" % e)
    finally:
        shutil.rmtree(path, ignore_errors=True)

    return package_installed, binaries_installed


def restart_pwnagotchi():
    # --no-block: this plugin runs inside the service that is about to be restarted
    subprocess.Popen(['systemctl', 'restart', '--no-block', PKG_SERVICE])


# ----------------------------------------------------------------- plugin ---

class AutoUpdate(plugins.Plugin):
    __author__ = 'evilsocket@gmail.com, NixPokeFun'
    __version__ = '2.1.0'
    __name__ = 'auto-update'
    __license__ = 'GPL3'
    __description__ = 'Updates bettercap/pwngrid and bundled pwnagotchi executables, plus the pwnagotchi package, with verification and rollback.'

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

                pkg_info = None
                if self.options.get('update_pwnagotchi', False):
                    try:
                        pkg_info = check_package(
                            session,
                            self.options.get('pwnagotchi_repo', PKG_DEFAULT_REPO),
                            self.options.get('pwnagotchi_branch', PKG_DEFAULT_BRANCH))
                        if pkg_info:
                            components = []
                            if pkg_info['update_package']:
                                components.append('package')
                            if pkg_info['update_binaries']:
                                components.append('bin/pwnagotchi and bin/pwnygen')
                            logging.warning("[update] pwnagotchi %s update to %s (%s)" % (
                                ' + '.join(components), pkg_info['label'], pkg_info['message']))
                    except Exception as e:
                        logging.error("[update] pwnagotchi package: %s" % e)

                installed = 0
                pkg_installed = False
                pkg_bins_installed = False
                source_updates = 0
                if pkg_info:
                    source_updates = int(pkg_info['update_package']) + int(pkg_info['update_binaries'])
                num_updates = len(to_install) + source_updates
                if num_updates:
                    if do_install:
                        for info in to_install:
                            plugins.on('updating')
                            try:
                                install(agent, display, info, work_root)
                                installed += 1
                            except Exception as e:
                                logging.error("[update] failed to update %s: %s" % (info['name'], e))
                        # Apply the Python package and bundled executables after standalone services.
                        if pkg_info:
                            plugins.on('updating')
                            try:
                                live_dir = None
                                if pkg_info['update_package']:
                                    live_dir = resolve_package_dir(self.options.get('pwnagotchi_dir'))
                                bin_dir = self.options.get('pwnagotchi_bin_dir', PKG_BIN_DIR)
                                pkg_installed, pkg_bins_installed = install_package(
                                    display, pkg_info, work_root, live_dir, bin_dir)
                            except Exception as e:
                                logging.error("[update] failed to update pwnagotchi source: %s" % e)
                    else:
                        prev_status = '%d new update%s available!' % (
                            num_updates, 's' if num_updates > 1 else '')

                logging.info("[update] done")
                self.status.update()

                if (installed or pkg_installed or pkg_bins_installed) and self.options.get('reboot', False):
                    display.update(force=True, new_data={'status': 'Rebooting ...'})
                    pwnagotchi.reboot()
                elif pkg_installed or pkg_bins_installed:
                    # Updated package code or executables take effect after the service restarts.
                    display.update(force=True, new_data={'status': 'Restarting ...'})
                    restart_pwnagotchi()
                    return

            except Exception as e:
                logging.error("[update] %s" % e)

            display.update(force=True, new_data={'status': prev_status if prev_status is not None else ''})
