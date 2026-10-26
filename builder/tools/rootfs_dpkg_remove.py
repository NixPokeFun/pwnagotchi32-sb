#!/usr/bin/env python3
"""Remove apt/dpkg packages from a MOUNTED rootfs, without qemu and without
running maintainer scripts (the rootfs is armhf, the host is not).

It does what 'dpkg --purge' does to the filesystem and to dpkg's database:
  * deletes every file listed in <root>/var/lib/dpkg/info/<pkg>.list
  * removes now-empty directories that no other package owns
  * deletes the package's info files (.list, .md5sums, .postinst, ...)
  * removes its stanza from <root>/var/lib/dpkg/status

Dry run by default:

  sudo python3 rootfs_dpkg_remove.py --root /mnt/pwn_root ansible ansible-base
  sudo python3 rootfs_dpkg_remove.py --root /mnt/pwn_root ansible ansible-base --apply

Safety: refuses if a package that stays installed Depends/Pre-Depends on one
you remove (override with --force). Provides:/virtual packages are not
resolved, so read the report. Maintainer scripts are NOT executed; scripts
that touch update-alternatives or system users are listed as warnings.
"""
import argparse
import os
import re
import shutil
import sys

SUFFIXES = ("list", "md5sums", "conffiles", "shlibs", "symbols", "triggers",
            "preinst", "postinst", "prerm", "postrm", "config", "templates")


def read_status(path):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        text = f.read()
    stanzas = [s for s in text.split("\n\n") if s.strip()]
    parsed = []
    for s in stanzas:
        fields = {}
        key = None
        for line in s.split("\n"):
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line
            elif ":" in line:
                key, _, val = line.partition(":")
                fields[key] = val.strip()
        parsed.append((s, fields))
    return parsed


def dep_groups(value):
    groups = []
    for part in value.split(","):
        alts = []
        for alt in part.split("|"):
            name = re.split(r"[ (]", alt.strip(), maxsplit=1)[0]
            name = name.split(":")[0]
            if name:
                alts.append(name)
        if alts:
            groups.append(alts)
    return groups


def inside(root, path):
    return os.path.commonpath([root, path]) == root


def prune_dir(d):
    try:
        if os.listdir(d) == ["__pycache__"]:
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
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    admin = os.path.join(root, "var/lib/dpkg")
    status_path = os.path.join(admin, "status")
    info = os.path.join(admin, "info")
    if not os.path.isfile(status_path):
        sys.exit("no dpkg status file under %s" % admin)

    stanzas = read_status(status_path)
    installed = {}
    for s, f in stanzas:
        st = f.get("Status", "")
        if f.get("Package") and "not-installed" not in st and "config-files" not in st:
            installed[f["Package"]] = f
    targets = {}
    for p in a.packages:
        if p in installed:
            targets[p] = installed[p]
        else:
            print("[skip] not installed: %s" % p)
    if not targets:
        sys.exit("nothing to do")

    # ---- reverse dependency check ----
    blockers = []
    for name, f in installed.items():
        if name in targets:
            continue
        for field in ("Depends", "Pre-Depends"):
            for alts in dep_groups(f.get(field, "")):
                if not any(x in targets for x in alts):
                    continue
                other = [x for x in alts if x not in targets and x in installed]
                if not other:
                    blockers.append((name, field, " | ".join(alts)))
    if blockers:
        print("!! Still needed by packages that stay installed:")
        for name, field, dep in blockers:
            print("   %-28s %-11s %s" % (name, field + ":", dep))
        if not a.force:
            sys.exit("Aborting (remove the dependents too, or use --force).")

    # ---- directories owned by packages that stay ----
    keep_dirs = set()
    for fn in os.listdir(info):
        if not fn.endswith(".list"):
            continue
        base = fn[:-5]
        if base.split(":")[0] in targets:
            continue
        with open(os.path.join(info, fn), encoding="utf-8", errors="surrogateescape") as f:
            for line in f:
                keep_dirs.add(os.path.normpath(os.path.join(root, line.strip().lstrip("/"))))

    # ---- plan ----
    files, dirs, info_files, warnings = [], [], [], []
    for name, f in targets.items():
        arch = f.get("Architecture", "")
        bases = [name, "%s:%s" % (name, arch)]
        listed = False
        for b in bases:
            lp = os.path.join(info, b + ".list")
            if not os.path.isfile(lp):
                continue
            listed = True
            with open(lp, encoding="utf-8", errors="surrogateescape") as lf:
                for line in lf:
                    line = line.strip()
                    if not line or line == "/.":
                        continue
                    full = os.path.normpath(os.path.join(root, line.lstrip("/")))
                    if not inside(root, full) or full == root:
                        continue
                    if os.path.islink(full) or os.path.isfile(full):
                        files.append(full)
                    elif os.path.isdir(full):
                        dirs.append(full)
        if not listed:
            warnings.append("%s: no info/*.list found, only dpkg metadata is removed" % name)
        for b in bases:
            for suf in SUFFIXES:
                p = os.path.join(info, "%s.%s" % (b, suf))
                if os.path.isfile(p):
                    info_files.append(p)
                    if suf in ("preinst", "postinst", "prerm", "postrm"):
                        with open(p, encoding="utf-8", errors="replace") as sf:
                            body = sf.read()
                        for word in ("update-alternatives", "adduser", "useradd",
                                     "systemctl", "deb-systemd-helper", "dpkg-divert"):
                            if word in body:
                                warnings.append("%s.%s uses %s (not executed)" % (b, suf, word))

    files = sorted(set(files))
    print("\nPackages : %s" % ", ".join(sorted(targets)))
    print("Files    : %d" % len(files))
    print("Info     : %d dpkg metadata files" % len(info_files))
    for w in sorted(set(warnings)):
        print("[warn] %s" % w)
    if not a.apply:
        print("\nDry run only. Re-run with --apply to delete.")
        return

    # ---- apply ----
    for f in files:
        os.remove(f)
    removed_dirs = 0
    cand = set(dirs)
    for f in files:
        d = os.path.dirname(f)
        while inside(root, d) and d != root:
            cand.add(d)
            d = os.path.dirname(d)
    for d in sorted(cand, key=lambda x: x.count(os.sep), reverse=True):
        if d in keep_dirs or not os.path.isdir(d) or os.path.islink(d):
            continue
        if prune_dir(d):
            removed_dirs += 1
    for p in info_files:
        os.remove(p)

    backup = status_path + ".pre-offline-remove"
    if not os.path.exists(backup):
        shutil.copy2(status_path, backup)
    keep = [s.rstrip("\n") for s, f in stanzas if not (f.get("Package") in targets
                                           and "not-installed" not in f.get("Status", ""))]
    with open(status_path, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n\n".join(keep) + "\n\n")
    print("\nDeleted %d files, %d empty directories, %d info files." %
          (len(files), removed_dirs, len(info_files)))
    print("dpkg status updated (backup: %s)" % backup)


if __name__ == "__main__":
    main()
