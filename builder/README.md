# Pwnagotchi-ai32 image builder

Builds a Pi Zero W (armv6) image from the official **Raspberry Pi OS Lite, Debian 11 "bullseye", 32-bit**
(2023-05-03, kernel 6.1.21) and installs this repo into it. Status: in development.

```
./build.sh --docker              # recommended (WSL2 + Docker Desktop)
sudo ./build.sh --native         # Linux / WSL with the tools installed
./build.sh --dry-run             # print every step, change nothing
./build.sh --docker --only python    # re-run a single stage
```

## Directories

| Dir | Purpose |
|---|---|
| `builder/assets`, `brcm`, `data` | inputs (caplets, boot config, debs, firmware, files copied to `/`) |
| `builder/cache` | downloads: base image, artifacts (kept) |
| `builder/work` | working image and mount points (disposable) |
| `builder/out` | finished `*.img.xz` + `.sha256` |

Override with `--work-dir/--out-dir/--cache-dir` or in `config.local.env`. On WSL put `work` on a Linux
path (`--work-dir ~/pwn-work`), not on `/mnt/d`.

## Stages

`fetch prepare system apt firmware files artifacts python services prune verify compress`
(`./build.sh stages`). Mounting is automatic; `./build.sh mount|umount|shell` for manual work.

## Inputs you still have to provide

* `packages.apt` - apt packages. Take them from your working image:
  ```bash
  R=/mnt/pwn_root
  dpkg-query --admindir=$R/var/lib/dpkg -W -f='${Package}\n' | sort > /tmp/all.txt
  awk '/^Package:/{p=$2} /^Auto-Installed: 1/{print p}' $R/var/lib/apt/extended_states | sort -u > /tmp/auto.txt
  comm -23 /tmp/all.txt /tmp/auto.txt > /tmp/manual.txt   # compare with the same list from the base image
  ```
* `artifacts.list` - bettercap, pwngrid, libpcap (not stored in the repo).
* `brcm/brcmfmac.ko.xz` - patched nexmon driver for kernel 6.1.21+ (copy it from a working image:
  `usr/lib/modules/6.1.21+/kernel/drivers/net/wireless/broadcom/brcm80211/brcmfmac/`). Building nexmon
  inside the builder is not implemented yet.

## Notes

* **Target vs build container.** The image is bullseye; the Docker build container is bookworm on purpose:
  Debian 11 reached end of life on 31 Aug 2026 and moved to archive.debian.org.
* **apt on bullseye.** 32-bit Raspberry Pi OS uses the Raspbian repo (armv6 rebuilds). Never replace it with
  Debian's armhf archive: those binaries need armv7 and do not run on a Pi Zero.
* **Kernel and `firmware-brcm80211` are held** (`apt-mark hold`) so the nexmon driver and patched firmware are not overwritten by upgrades.
* **Line endings.** Add `.gitattributes` (see below) so Windows git does not turn scripts into CRLF.
* `tools/` contains the offline helpers (`rootfs_pip_check.py` is used by the `verify` stage).

## Suggested repo files

`.gitignore`:
```
builder/work/
builder/out/
builder/cache/
builder/config.local.env
```
`.gitattributes`:
```
*.sh text eol=lf
*.py text eol=lf
*.env text eol=lf
*.list text eol=lf
*.apt text eol=lf
Dockerfile text eol=lf
builder/data/etc/** text eol=lf
builder/data/usr/bin/* text eol=lf
builder/assets/bettercap/*.cap text eol=lf
```

## Updates in this version

* `EXTRA_BIN` (config.env): scripts from repo `bin/` copied as-is to `/usr/local/bin` (default `pwnygen`),
  they are *not* installed by pip.
* Artifacts: archives need the `member` column (a `.deb` is rejected with a hint). A `*.sha256` file packed
  next to the binary inside the zip (`hash  filename`) is checked against the extracted binary; pin the zip's
  sha256 in `artifacts.list` for real protection. That `.sha256` is also installed next to the binary
  (`INSTALL_BUNDLED_SHA=0` to disable).
* The artifact cache is keyed by URL, so two releases with the same asset name no longer collide.
* The apt stage aborts if `raspberrypi-kernel` changes (override: `ALLOW_KERNEL_CHANGE=1`).
* Docker is detected with `docker info` (a WSL stub without Docker Desktop integration is not enough).
