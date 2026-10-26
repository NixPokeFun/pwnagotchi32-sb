#!/usr/bin/env python3
"""Verify / repair the RECORD file of an installed *.dist-info directory.

Default mode only checks and reports. Nothing is written without --fix.

  python3 fix_record.py [path/to/pkg-X.Y.dist-info]          # check
  python3 fix_record.py [path/to/pkg-X.Y.dist-info] --fix    # repair
  python3 fix_record.py [path/to/pkg-X.Y.dist-info] --fix --prune

--fix updates size+hash of mismatching files inside the dist-info directory
(METADATA, direct_url.json, ...) and rewrites a stale dist-info prefix in
RECORD (e.g. 'pwnagotchi-2.8.7.2.dist-info/...') to the real directory name.
Add --all to also update hashes of mismatching package files.
Add --prune (with --fix) to drop RECORD entries of files that no longer exist
(e.g. after deleting a plugin), including their stale .pyc entries.
Add --add-new (with --fix) to append files that exist inside the package
directories but are not in RECORD yet (e.g. your own plugins). Unrecorded
files are always reported, .pyc files and __pycache__ are ignored.
A backup is saved as RECORD.bak the first time.
"""
import argparse
import base64
import csv
import hashlib
import os
import shutil
import sys


def digest(path):
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    enc = base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode()
    return "sha256=" + enc, n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("distinfo", nargs="?", default=".")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--add-new", action="store_true")
    a = ap.parse_args()

    di = os.path.abspath(a.distinfo)
    record = os.path.join(di, "RECORD")
    if not di.endswith(".dist-info") or not os.path.isfile(record):
        sys.exit("Run inside (or pass the path of) a *.dist-info directory with RECORD.")

    base = os.path.dirname(di)          # site-packages / dist-packages
    name = os.path.basename(di)

    with open(record, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))

    out, renamed, bad_di, bad_pkg, missing, fixed, pruned = [], 0, [], [], [], 0, []
    for row in rows:
        if len(row) != 3:
            out.append(row)
            continue
        path, h, size = row
        parts = path.split("/")
        in_di = parts[0].endswith(".dist-info")
        new_path = path
        if in_di and parts[0] != name:
            new_path = "/".join([name] + parts[1:])
            renamed += 1
        full = os.path.normpath(os.path.join(base, new_path))
        if not h:                        # RECORD itself, .pyc files
            if a.fix and a.prune and not os.path.isfile(full) \
                    and not new_path.endswith("/RECORD"):
                pruned.append(path)
                continue
            out.append([new_path, h, size])
            continue
        if not os.path.isfile(full):
            missing.append(path)
            if a.fix and a.prune:
                pruned.append(path)
                continue
            out.append([new_path, h, size])
            continue
        cur_h, cur_n = digest(full)
        if cur_h != h or str(cur_n) != size:
            (bad_di if in_di else bad_pkg).append(path)
            if a.fix and (in_di or a.all):
                h, size = cur_h, str(cur_n)
                fixed += 1
        out.append([new_path, h, size])

    # ---- files on disk that RECORD does not know about ----
    recorded = {r[0] for r in rows if len(r) == 3}
    pkg_roots = sorted({r[0].split("/")[0] for r in rows
                        if len(r) == 3 and not r[0].startswith("..")
                        and not r[0].split("/")[0].endswith(".dist-info")
                        and "/" in r[0]})
    unrecorded = []
    for top in pkg_roots:
        for dirpath, dirnames, filenames in os.walk(os.path.join(base, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if fn.endswith(".pyc"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), base).replace(os.sep, "/")
                if rel not in recorded:
                    unrecorded.append(rel)
    unrecorded.sort()
    added = 0
    if a.fix and a.add_new:
        for rel in unrecorded:
            h, n = digest(os.path.join(base, rel))
            out.append([rel, h, str(n)])
            added += 1

    print("dist-info dir      :", name)
    print("renamed prefixes   :", renamed, "(RECORD uses a different dist-info name)" if renamed else "")
    print("dist-info mismatch :", len(bad_di), *[("\n   " + p) for p in bad_di])
    print("package mismatch   :", len(bad_pkg), *[("\n   " + p) for p in bad_pkg[:20]])
    if len(bad_pkg) > 20:
        print("   ... and", len(bad_pkg) - 20, "more")
    print("missing files      :", len(missing), *[("\n   " + p) for p in missing[:20]])
    print("not in RECORD      :", len(unrecorded), *[("\n   " + p) for p in unrecorded[:20]])

    if not a.fix:
        print("\nCheck only. Re-run with --fix to repair.")
        return

    bak = record + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(record, bak)
    with open(record, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, lineterminator="\n").writerows(out)
    print("\nRECORD rewritten. updated entries:", fixed,
          "| pruned entries:", len(pruned), "| added entries:", added,
          "| backup:", bak)
    if bad_pkg and not a.all:
        print("Package-file mismatches were left as is (use --all to update them too).")


if __name__ == "__main__":
    main()
