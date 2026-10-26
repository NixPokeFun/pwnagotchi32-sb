#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from setuptools import setup, find_packages
from setuptools.command.install import install
import logging
import os
import re
import shutil
import warnings
import platform

log = logging.getLogger(__name__)

SETUP_PATH = os.path.dirname(os.path.abspath(__file__))

# System files shipped with the repo. Layout mirrors the target filesystem:
#   builder/data/etc/...  -> /etc/...
#   builder/data/usr/...  -> /usr/...
BUILDER_DATA = os.path.join(SETUP_PATH, "builder", "data")

# Files under builder/data (paths relative to it) that are only used by the
# image builder and must NOT be copied onto a running system by setup.py.
BUILDER_ONLY = {
    "etc/dhcpcd.conf.txt",
}

# Never overwrite these if they already exist on the system.
# https://github.com/evilsocket/pwnagotchi/issues/483
KEEP_EXISTING_PREFIXES = (
    "/etc/network/interfaces.d/",
)


def install_file(source_filename, dest_filename):
    # do not overwrite network configuration if it exists already
    if dest_filename.startswith(KEEP_EXISTING_PREFIXES) and os.path.exists(dest_filename):
        log.info(f"{dest_filename} exists, skipping ...")
        return

    log.info(f"installing {source_filename} to {dest_filename} ...")
    dest_folder = os.path.dirname(dest_filename)
    if not os.path.isdir(dest_folder):
        os.makedirs(dest_folder)

    shutil.copy2(source_filename, dest_filename)
    if dest_filename.startswith("/usr/bin/"):
        os.chmod(dest_filename, 0o755)


def install_system_files():
    # builder/data targets the 32-bit ARM image
    if not platform.machine().startswith("arm"):
        warnings.warn(
            f"Unsupported architecture '{platform.machine()}', "
            "skipping pwnagotchi system files!"
        )
        return

    if not os.path.isdir(BUILDER_DATA):
        warnings.warn(f"{BUILDER_DATA} not found, skipping pwnagotchi system files!")
        return

    for root, _, files in os.walk(BUILDER_DATA):
        for name in sorted(files):
            source_filename = os.path.join(root, name)
            rel = os.path.relpath(source_filename, BUILDER_DATA).replace(os.sep, "/")
            if rel in BUILDER_ONLY:
                log.info(f"{rel} is builder-only, skipping ...")
                continue
            install_file(source_filename, "/" + rel)


def restart_services():
    # reload systemd units
    os.system("systemctl daemon-reload")

    # for people updating https://github.com/evilsocket/pwnagotchi/pull/551/files
    os.system("systemctl enable fstrim.timer")


class CustomInstall(install):
    def run(self):
        super().run()
        if os.geteuid() != 0:
            warnings.warn(
                "Not running as root, can't install pwnagotchi system files!"
            )
            return
        install_system_files()
        restart_services()


def version(version_file):
    with open(version_file, 'rt') as vf:
        version_file_content = vf.read()

    version_match = re.search(r"__version__\s*=\s*[\"\']([^\"\']+)", version_file_content)
    if version_match:
        return version_match.groups()[0]

    return None


def read_requirements(path):
    """Parse requirements.txt: skip comments, blank lines and pip options.

    Inline comments are stripped only when preceded by whitespace, so URL
    fragments like '#sha256=...' stay intact.
    """
    requirements = []
    with open(path) as fp:
        for line in fp:
            line = re.sub(r"\s+#.*$", "", line.strip())
            if not line or line.startswith(("#", "-")):
                continue
            requirements.append(line)
    return requirements


required = read_requirements(os.path.join(SETUP_PATH, 'requirements.txt'))

VERSION_FILE = os.path.join(SETUP_PATH, 'pwnagotchi', '_version.py')
pwnagotchi_version = version(VERSION_FILE)

setup(name='pwnagotchi',
      version=pwnagotchi_version,
      description='(⌐■_■) - Deep Reinforcement Learning instrumenting bettercap for WiFI pwning.',
      author='evilsocket && the dev team',
      author_email='evilsocket@gmail.com',
      url='https://pwnagotchi.ai/',
      license='GPL',
      install_requires=required,
      cmdclass={
          "install": CustomInstall,
      },
      scripts=['bin/pwnagotchi'],
      package_data={'pwnagotchi': ['defaults.toml', 'pwnagotchi/defaults.toml', 'locale/*/LC_MESSAGES/*.mo']},
      include_package_data=True,
      packages=find_packages(),
      classifiers=[
          'Programming Language :: Python :: 3',
          'Development Status :: 5 - Production/Stable',
          'License :: OSI Approved :: GNU General Public License (GPL)',
          'Environment :: Console',
      ])
