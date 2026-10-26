#!/usr/bin/env python3
"""Offline 'pip check' for a MOUNTED rootfs (no qemu, no booting).

Reads the metadata of every installed distribution under the given site dirs
and checks that each non-extra Requires-Dist is present and satisfied.
Environment markers are evaluated for the TARGET (Python 3.9, armv6l Linux),
not for the host.

  python3 rootfs_pip_check.py --root /mnt/pwn_root

Requirements given as direct URLs (torch @ https://...) are only checked for
presence. Distributions with a non-PEP 440 version (e.g. 'ai32') are reported
as a note, their own requirements are still checked.
"""
import argparse
import os
import re
import sys

try:
    from packaging.requirements import Requirement, InvalidRequirement
    from packaging.version import Version, InvalidVersion
except ImportError:  # fall back to pip's vendored copy
    from pip._vendor.packaging.requirements import Requirement, InvalidRequirement
    from pip._vendor.packaging.version import Version, InvalidVersion


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_meta(dist_dir):
    # Debian ships distutils packages as a single *.egg-info FILE (PKG-INFO
    # format), e.g. python3-yaml and python3-rpi.gpio; others as directories.
    candidates = [dist_dir] if os.path.isfile(dist_dir) else [
        os.path.join(dist_dir, "METADATA"), os.path.join(dist_dir, "PKG-INFO")]
    for path in candidates:
        if os.path.isfile(path):
            name = version = None
            reqs = []
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip() == "":
                        break                     # end of headers
                    if line.startswith("Name:") and name is None:
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("Version:") and version is None:
                        version = line.split(":", 1)[1].strip()
                    elif line.startswith("Requires-Dist:"):
                        reqs.append(line.split(":", 1)[1].strip())
            return name, version, reqs
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--root", default="/mnt/pwn_root")
    ap.add_argument("--site", action="append",
                    help="site dir relative to --root (repeatable)")
    ap.add_argument("--machine", default="armv6l")
    ap.add_argument("--python", default="3.9.2")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    sites = a.site or ["usr/local/lib/python3.9/dist-packages",
                       "usr/lib/python3/dist-packages"]
    py_short = ".".join(a.python.split(".")[:2])
    env = {
        "python_version": py_short,
        "python_full_version": a.python,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "implementation_version": a.python,
        "os_name": "posix",
        "sys_platform": "linux",
        "platform_system": "Linux",
        "platform_machine": a.machine,
        "platform_release": "",
        "platform_version": "",
        "extra": "",
    }

    dists, dupes = {}, []
    for rel in sites:
        site = os.path.join(root, rel)
        if not os.path.isdir(site):
            print("[note] site dir not found: %s" % rel)
            continue
        for d in sorted(os.listdir(site)):
            full = os.path.join(site, d)
            if not (d.endswith((".dist-info", ".egg-info"))
                    and (os.path.isdir(full) or
                         (d.endswith(".egg-info") and os.path.isfile(full)))):
                continue
            meta = read_meta(full)
            if not meta or not meta[0]:
                continue
            name, version, reqs = meta
            key = norm(name)
            if key in dists:
                dupes.append((name, dists[key]["site"], rel))
            dists[key] = dict(name=name, version=version, reqs=reqs, site=rel)

    problems, notes = [], []
    for key, d in sorted(dists.items()):
        try:
            Version(d["version"] or "")
        except InvalidVersion:
            notes.append("%s has a non-PEP 440 version '%s' (pip >= 24.1 ignores such packages)"
                         % (d["name"], d["version"]))
        for line in d["reqs"]:
            try:
                req = Requirement(line)
            except InvalidRequirement:
                problems.append("%s: cannot parse requirement %r" % (d["name"], line))
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate(env):
                        continue
                except Exception:
                    pass
                # extras-only requirements are not required
                if "extra" in str(req.marker):
                    continue
            target = dists.get(norm(req.name))
            if target is None:
                problems.append("%s requires %s, which is NOT installed"
                                % (d["name"], str(req).split(";")[0].strip()))
                continue
            if req.url:
                continue
            try:
                ver = Version(target["version"])
            except InvalidVersion:
                continue
            if req.specifier and not req.specifier.contains(ver, prereleases=True):
                problems.append("%s requires %s%s, but %s is installed"
                                % (d["name"], req.name, req.specifier, ver))

    print("distributions checked :", len(dists))
    if dupes:
        print("present in both sites :")
        for n, s1, s2 in dupes:
            print("   %-24s %s  +  %s" % (n, s1, s2))
    for n in notes:
        print("[note]", n)
    if problems:
        print("\nPROBLEMS (%d):" % len(problems))
        for p in problems:
            print("   -", p)
        sys.exit(1)
    print("\nNo broken requirements found.")


if __name__ == "__main__":
    main()
