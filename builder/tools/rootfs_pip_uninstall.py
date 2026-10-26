#!/usr/bin/env python3
"""Uninstall pip packages from a MOUNTED rootfs, without pip and without qemu.

Works like 'pip uninstall' does: reads each package's dist-info/RECORD and
removes exactly the files listed there (plus its *-nspkg.pth), then removes
directories that became empty. Nothing outside --root is ever touched.

Dry run by default:

  sudo python3 rootfs_pip_uninstall.py --root /mnt/pwn_root PyDrive2 oauth2client ...
  sudo python3 rootfs_pip_uninstall.py --root /mnt/pwn_root PyDrive2 oauth2client ... --apply

Safety check: before removing, the METADATA of every package that stays
installed (in --site and in <root>/usr/lib/python3/dist-packages) is scanned.
If one of them still requires a package you are removing, the script stops
(override with --force). Update pwnagotchi's METADATA first.
"""
import argparse
import glob
import os
import re
import shutil
import sys


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def list_dists(site):
    """{normalized name: dist-info/egg-info directory path}"""
    found = {}
    if not os.path.isdir(site):
        return found
    for d in sorted(os.listdir(site)):
        full = os.path.join(site, d)
        if d.endswith((".dist-info", ".egg-info")) and os.path.isdir(full):
            stem = d.rsplit(".", 1)[0]
            found[norm(stem.split("-")[0])] = full
    return found


def requires_of(dist_dir):
    for fname in ("METADATA", "PKG-INFO"):
        path = os.path.join(dist_dir, fname)
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("Requires-Dist:"):
                        yield line.split(":", 1)[1].strip()
            return


def inside(root, path):
    return os.path.commonpath([root, path]) == root


def prune_dir(d):
    """Remove directory d if empty (a lone __pycache__ of .pyc counts as empty)."""
    try:
        entries = os.listdir(d)
        if entries == ["__pycache__"]:
            pyc = os.path.join(d, "__pycache__")
            if all(n.endswith(".pyc") for n in os.listdir(pyc)):
                shutil.rmtree(pyc)
        os.rmdir(d)
        return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("packages", nargs="+")
    ap.add_argument("--root", default="/mnt/pwn_root")
    ap.add_argument("--site", default="usr/local/lib/python3.9/dist-packages",
                    help="site dir relative to --root")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    site = os.path.join(root, a.site)
    if not os.path.isdir(site):
        sys.exit("site dir not found: %s" % site)

    dists = list_dists(site)
    targets = {}
    for p in a.packages:
        k = norm(p)
        if k in dists:
            targets[k] = dists[k]
        else:
            print("[skip] not installed in site dir: %s" % p)
    if not targets:
        sys.exit("nothing to do")

    # ---- reverse dependency check ----
    blockers = []
    scan = [dists, list_dists(os.path.join(root, "usr/lib/python3/dist-packages"))]
    for table in scan:
        for dname, dpath in table.items():
            if dname in targets:
                continue
            for req in requires_of(dpath):
                if "extra ==" in req or "extra==" in req:
                    continue
                m = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", req)
                if m and norm(m.group(0)) in targets:
                    blockers.append((dname, req))
    if blockers:
        print("!! Still required by packages that stay installed:")
        for dname, req in blockers:
            print("   %-24s requires  %s" % (dname, req))
        if not a.force:
            sys.exit("Aborting (fix METADATA of the dependent package or use --force).")

    # ---- plan ----
    files, dirs_to_prune = [], set()
    for k, ddir in targets.items():
        record = os.path.join(ddir, "RECORD")
        stem = os.path.basename(ddir).rsplit(".", 1)[0]
        paths = []
        if os.path.isfile(record):
            with open(record, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if line:
                        paths.append(line.split(",")[0])
        else:
            print("[warn] %s has no RECORD, removing only its dist-info" % k)
        paths += [os.path.relpath(p, site) for p in
                  glob.glob(os.path.join(site, stem + "-*nspkg.pth"))]
        for rel in paths:
            full = os.path.abspath(os.path.join(site, rel))
            if not inside(root, full):
                print("[skip] outside root: %s" % rel)
                continue
            if os.path.lexists(full) and not os.path.isdir(full):
                files.append(full)
        dirs_to_prune.add(ddir)

    files = sorted(set(files))
    print("\nPackages : %s" % ", ".join(sorted(targets)))
    print("Files    : %d" % len(files))
    for f in files:
        if not os.path.abspath(f).startswith(site + os.sep):
            print("   (outside site) %s" % os.path.relpath(f, root))
    if not a.apply:
        print("\nDry run only. Re-run with --apply to delete.")
        return

    # ---- apply ----
    for f in files:
        os.remove(f)
        d = os.path.dirname(f)
        # only prune directories inside the site dir, never above it
        while d.startswith(site + os.sep):
            dirs_to_prune.add(d)
            d = os.path.dirname(d)
    # the dist-info/egg-info directory of an uninstalled package goes entirely
    for ddir in targets.values():
        if os.path.isdir(ddir):
            shutil.rmtree(ddir)
    removed = 0
    for d in sorted(dirs_to_prune, key=lambda x: x.count(os.sep), reverse=True):
        if os.path.isdir(d) and prune_dir(d):
            removed += 1
    print("\nDeleted %d files, %d empty directories." % (len(files), removed))
    left = [os.path.relpath(d, root) for d in dirs_to_prune if os.path.exists(d)]
    if left:
        print("Directories kept (not empty):")
        for d in sorted(left):
            print("   %s" % d)


if __name__ == "__main__":
    main()
