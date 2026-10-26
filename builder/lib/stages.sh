# shellcheck shell=bash
# ---------------------------------------------------------------------------
#  build stages. Every stage mounts the image on demand (ensure_mounted) and
#  is safe to run on its own:   ./build.sh --only python
# ---------------------------------------------------------------------------

ALL_STAGES=(fetch prepare system apt firmware files artifacts python services prune verify compress)

stage_enabled() {
  local s=$1 x
  if [[ -n ${ONLY_STAGES:-} ]]; then
    for x in ${ONLY_STAGES//,/ }; do [[ $x == "$s" ]] && return 0; done
    return 1
  fi
  for x in ${SKIP_STAGES//,/ }; do [[ $x == "$s" ]] && return 1; done
  return 0
}

detect_kernel() {
  if [[ -n ${KERNEL_RELEASE:-} ]]; then return 0; fi
  if (( DRY_RUN )); then KERNEL_RELEASE="6.1.21+"; return 0; fi
  KERNEL_RELEASE="$(ls "$MNT_ROOT/usr/lib/modules" 2>/dev/null \
                    | grep -E '^[0-9][0-9.]*\+$' | sort -V | tail -n1 || true)"
  [[ -n $KERNEL_RELEASE ]] || die "cannot detect the armv6 kernel in $MNT_ROOT/usr/lib/modules (set KERNEL_RELEASE)"
  info "kernel release: $KERNEL_RELEASE"
}

# ---- system: hostname, user, ssh ---------------------------------------------
stage_system() {
  step "System: hostname, user, ssh"
  ensure_mounted
  write_file "$MNT_ROOT/etc/hostname" "$TARGET_HOSTNAME"
  run sed -i -E "s/^(127\.0\.1\.1[[:space:]]+).*/\1${TARGET_HOSTNAME}/" "$MNT_ROOT/etc/hosts"
  if (( CREATE_USER )); then
    local hash
    if (( DRY_RUN )); then hash='$6$dryrun'; else hash="$(openssl passwd -6 "$TARGET_PASSWORD")"; fi
    chroot_script user "B_USER=$TARGET_USER" "B_HASH=$hash" <<'SCRIPT'
set -e
if ! id -u "$B_USER" >/dev/null 2>&1; then
  groups=""
  for g in sudo adm dialout cdrom audio video plugdev games users input netdev gpio i2c spi; do
    if getent group "$g" >/dev/null; then groups="${groups:+$groups,}$g"; fi
  done
  useradd -m -s /bin/bash -G "$groups" "$B_USER"
fi
echo "$B_USER:$B_HASH" | chpasswd -e
systemctl enable ssh || true
SCRIPT
    if [[ $TARGET_PASSWORD == raspberry ]]; then
      warn "default password in use for '$TARGET_USER'. Set TARGET_PASSWORD in config.local.env."
    fi
  fi
}

# ---- apt ---------------------------------------------------------------------
kernel_pkg_version() {
  if (( DRY_RUN )); then printf 'dry-run'; return 0; fi
  chroot_exec dpkg-query -W -f='${Version}' raspberrypi-kernel 2>/dev/null || true
}

apt_hint() {
  warn "apt-get update failed. Debian 11 'bullseye' reached end of life on 31 Aug 2026 and was moved to"
  warn "archive.debian.org. 32-bit Raspberry Pi OS uses the Raspbian repo (armv6 rebuilds) - do NOT point"
  warn "it at Debian's armhf archive, those binaries need armv7 and will not run on a Pi Zero."
  warn "Options: set APT_SOURCES_FILE to a working sources.list, or build without apt packages."
}

stage_apt() {
  step "apt packages"
  ensure_mounted
  local pkgs=() deb
  mapfile -t pkgs < <(grep -vE '^[[:space:]]*(#|$)' "$BUILDER_DIR/packages.apt" 2>/dev/null | awk '{print $1}')
  local debs=("$BUILDER_DIR"/assets/packages/*.deb)
  if (( ${#pkgs[@]} == 0 )) && { [[ ! -e ${debs[0]} ]] || (( ! INSTALL_LOCAL_DEBS )); }; then
    warn "packages.apt is empty and there are no local .deb files, skipping"
    return 0
  fi
  if [[ -n ${APT_SOURCES_FILE:-} ]]; then
    info "using APT_SOURCES_FILE=$APT_SOURCES_FILE"
    run cp "$APT_SOURCES_FILE" "$MNT_ROOT/etc/apt/sources.list"
  fi
  # keep the kernel and the Wi-Fi firmware package the nexmon patches were made for
  local hp
  for hp in raspberrypi-kernel raspberrypi-kernel-headers raspberrypi-bootloader firmware-brcm80211; do
    chroot_exec apt-mark hold "$hp" || true
  done
  chroot_exec apt-get update || { apt_hint; die "apt-get update failed"; }
  local kver_before
  kver_before="$(kernel_pkg_version)"
  if (( ${#pkgs[@]} )); then
    chroot_exec apt-get install -y --allow-change-held-packages --no-install-recommends "${pkgs[@]}"
  fi
  if (( INSTALL_LOCAL_DEBS )) && [[ -e ${debs[0]} ]]; then
    run mkdir -p "$MNT_ROOT/tmp/builder-debs"
    for deb in "${debs[@]}"; do run cp "$deb" "$MNT_ROOT/tmp/builder-debs/"; done
    chroot_exec bash -c 'apt-get install -y /tmp/builder-debs/*.deb'
    run rm -rf "$MNT_ROOT/tmp/builder-debs"
  fi
  local kver_after
  kver_after="$(kernel_pkg_version)"
  if [[ $kver_before != "$kver_after" && ${ALLOW_KERNEL_CHANGE:-0} != 1 ]]; then
    die "raspberrypi-kernel changed during apt ($kver_before -> $kver_after). The nexmon driver is built for the old kernel. Pin versions in packages.apt (e.g. raspberrypi-kernel-headers=$kver_before) or set ALLOW_KERNEL_CHANGE=1."
  fi
  chroot_exec apt-get clean
}

# ---- firmware: patched brcm firmware + optional prebuilt nexmon driver --------
stage_firmware() {
  step "Wi-Fi firmware (brcm / nexmon)"
  ensure_mounted
  detect_kernel
  local fw="$MNT_ROOT/usr/lib/firmware/brcm" f base n=0
  run mkdir -p "$fw"
  for f in "$BUILDER_DIR"/brcm/*.bin "$BUILDER_DIR"/brcm/*.txt "$BUILDER_DIR"/brcm/*.hcd; do
    [[ -e $f ]] || continue
    base="$(basename "$f")"
    if [[ -e $fw/$base && ! -e $fw/$base.orig ]]; then run cp -a "$fw/$base" "$fw/$base.orig"; fi
    run install -m 0644 "$f" "$fw/$base"
    n=$((n + 1))
  done
  ok "installed $n firmware file(s)"

  local drvdir="$MNT_ROOT/usr/lib/modules/$KERNEL_RELEASE/kernel/drivers/net/wireless/broadcom/brcm80211/brcmfmac"
  local drv=""
  for f in "$BUILDER_DIR"/brcm/brcmfmac.ko.xz "$BUILDER_DIR"/brcm/brcmfmac.ko; do
    if [[ -e $f ]]; then drv=$f; break; fi
  done
  if [[ -n $drv ]]; then
    base="$(basename "$drv")"
    if [[ -e $drvdir/brcmfmac.ko.xz && ! -e $drvdir/brcmfmac.ko.xz.orig ]]; then
      run mv "$drvdir/brcmfmac.ko.xz" "$drvdir/brcmfmac.ko.xz.orig"
    fi
    run install -m 0644 "$drv" "$drvdir/$base"
    run depmod -b "$MNT_ROOT" "$KERNEL_RELEASE"
    ok "patched driver installed ($base), depmod done"
  else
    warn "no prebuilt brcmfmac.ko(.xz) in builder/brcm - monitor mode/injection will not work."
    warn "Building nexmon inside the builder is not implemented yet (see brcm/nexmon.yml for the recipe)."
  fi
}

# ---- files: builder/data tree + assets ---------------------------------------
# Files in data/ that are only used by the builder (setup.py skips them too)
DATA_EXCLUDES=(etc/dhcpcd.conf.txt)

stage_files() {
  step "Files: data tree, boot config, caplets"
  ensure_mounted
  local ex=() e
  for e in "${DATA_EXCLUDES[@]}"; do ex+=(--exclude "/$e"); done
  run rsync -rlpt --chown=root:root "${ex[@]}" "$BUILDER_DIR/data/" "$MNT_ROOT/"
  if [[ -d $BUILDER_DIR/data/usr/bin ]] && ! dry "chmod 0755 usr/bin/* (scripts)"; then
    find "$BUILDER_DIR/data/usr/bin" -type f -printf '%f\n' | while read -r f; do chmod 0755 "$MNT_ROOT/usr/bin/$f"; done
  fi
  run install -m 0644 "$BUILDER_DIR/data/etc/dhcpcd.conf.txt" "$MNT_ROOT/etc/dhcpcd.conf"

  # /boot/config.txt (keep the original once)
  if [[ -e $MNT_ROOT/boot/config.txt && ! -e $MNT_ROOT/boot/config.txt.orig ]]; then
    run cp -a "$MNT_ROOT/boot/config.txt" "$MNT_ROOT/boot/config.txt.orig"
  fi
  run install -m 0755 "$BUILDER_DIR/assets/boot/config.txt" "$MNT_ROOT/boot/config.txt"

  # bettercap caplets used by bettercap-launcher
  run mkdir -p "$MNT_ROOT/usr/local/share/bettercap/caplets"
  local c
  for c in "$BUILDER_DIR"/assets/bettercap/*.cap; do
    [[ -e $c ]] || continue
    run install -m 0644 "$c" "$MNT_ROOT/usr/local/share/bettercap/caplets/$(basename "$c")"
  done
  ok "files installed"
}

# ---- artifacts: binaries from artifacts.list ---------------------------------
# fetch_artifact URL SHA256 -> sets ARTIFACT_FILE (a global: log lines must not end up in a $(...) capture)
ARTIFACT_FILE=""
fetch_artifact() {
  local url=$1 sha=$2 key f
  # the URL hash keeps two releases with the same asset name apart in the cache
  key="$(printf '%s' "$url" | sha256sum | cut -c1-8)"
  f="$CACHE_DIR/artifacts/${key}-${url##*/}"
  if ! dry "download $url"; then
    mkdir -p "$CACHE_DIR/artifacts"
    if [[ ! -f $f ]]; then
      curl -fL --retry 3 -o "$f.part" "$url" || die "download failed: $url"
      mv "$f.part" "$f"
    fi
    if [[ $sha != - ]]; then
      [[ "$(sha256sum "$f" | awk '{print $1}')" == "$sha" ]] || die "checksum mismatch for $f (delete it to re-download)"
    else
      warn "no sha256 pinned for $url"
    fi
  fi
  ARTIFACT_FILE=$f
}

# A *.sha256 file packed next to the binary (format: "hash  filename") is
# checked against the extracted binary. This catches broken/mispacked
# archives; it is NOT protection against a malicious release (pin the zip in
# the sha256 column for that).
BUNDLED_SHA_FILE=""   # set by verify_bundled_sha: the .sha256 file that matched
verify_bundled_sha() {   # verify_bundled_sha EXTRACTED_FILE
  local src=$1 base dir sf h n found=0
  BUNDLED_SHA_FILE=""
  base="$(basename "$src")"; dir="$(dirname "$src")"
  for sf in "$dir"/*.sha256; do
    [[ -e $sf ]] || continue
    while read -r h n _; do
      h="${h%$'\r'}"; n="${n:-}"; n="${n%$'\r'}"; n="${n#\*}"; n="$(basename "${n:-.}")"
      [[ -n $h && $n == "$base" ]] || continue
      found=1
      [[ "$(sha256sum "$src" | awk '{print $1}')" == "$h" ]] \
        || die "bundled checksum mismatch for $base (${sf##*/})"
      ok "$base matches bundled ${sf##*/}"
      BUNDLED_SHA_FILE=$sf
    done < "$sf"
  done
  if (( ! found )); then info "no bundled .sha256 entry for $base"; fi
}

stage_artifacts() {
  step "Artifacts (bettercap, pwngrid, ...)"
  ensure_mounted
  local list="$BUILDER_DIR/artifacts.list" n=0 name url sha dest mode member f src tmp
  if [[ ! -f $list ]]; then info "no artifacts.list, skipping"; return 0; fi
  while read -r name url sha dest mode member; do
    [[ -z ${name:-} || $name == \#* ]] && continue
    n=$((n + 1))
    info "$name"
    fetch_artifact "$url" "$sha"
    f=$ARTIFACT_FILE
    src=$f
    BUNDLED_SHA_FILE=""
    case "$f" in
      *.zip|*.tar.gz|*.tgz)
        [[ -n ${member:-} ]] || die "$name: $url is an archive, add the file name inside it as the last column"
        if dry "extract $member from ${f##*/}, check bundled .sha256"; then
          src="$WORK_DIR/artifact-$name/$member"
        else
          tmp="$WORK_DIR/artifact-$name"; rm -rf "$tmp"; mkdir -p "$tmp"
          case "$f" in
            *.zip) unzip -q -o "$f" -d "$tmp" ;;
            *)     tar -xzf "$f" -C "$tmp" ;;
          esac
          src="$tmp/$member"
          [[ -f $src ]] || die "$name: '$member' not found inside ${f##*/}"
          verify_bundled_sha "$src"
        fi ;;
      *.deb) die "$name: .deb files are not supported, pack the file into a .zip and give its name as the last column" ;;
      *)     [[ -z ${member:-} ]] || die "$name: a member name is given but $url is not a .zip/.tar.gz" ;;
    esac
    run install -D -m "${mode:-0755}" "$src" "$MNT_ROOT$dest"
    # the .sha256 that verified the binary is installed next to it (same name as in the zip);
    # `cd <dir> && sha256sum -c <file>` works inside the image if the dest file name == the name in the .sha256
    if (( ${INSTALL_BUNDLED_SHA:-1} )); then
      if (( DRY_RUN )); then
        dry "install the bundled .sha256 (if any) next to $dest"
      elif [[ -n $BUNDLED_SHA_FILE ]]; then
        run install -D -m 0644 "$BUNDLED_SHA_FILE" "$MNT_ROOT$(dirname "$dest")/$(basename "$BUNDLED_SHA_FILE")"
        info "installed $(dirname "$dest")/$(basename "$BUNDLED_SHA_FILE")"
      fi
    fi
  done < "$list"
  if (( n == 0 )); then
    warn "artifacts.list has no entries: bettercap/pwngrid/libpcap are NOT installed (see README)"
  else
    chroot_exec ldconfig
  fi
}

# ---- python: pinned pip, torch wheels, requirements, pwnagotchi ---------------
stage_python() {
  step "Python"
  ensure_mounted
  local req="$REPO_ROOT/requirements.txt" wheels=() w
  [[ -f $req ]] || die "requirements.txt not found in $REPO_ROOT"
  info "python / machine as seen under qemu:"
  chroot_exec python3 -c 'import platform, sys; print("    ", sys.version.split()[0], platform.machine())'
  chroot_script pip-bootstrap <<'SCRIPT'
set -e
python3 -m pip --version >/dev/null 2>&1 || apt-get install -y python3-pip
grep -qs piwheels /etc/pip.conf || printf '[global]\nextra-index-url=https://www.piwheels.org/simple\n' >> /etc/pip.conf
SCRIPT
  # shellcheck disable=SC2086
  chroot_exec python3 -m pip install --no-cache-dir --upgrade $PIP_PIN

  # direct-URL wheels for armv6l (torch, torchvision): install first, the
  # platform marker may not match under qemu
  mapfile -t wheels < <(sed -nE 's/^[A-Za-z0-9_.-]+ @ (https?:[^ ;]+).*armv6l.*/\1/p' "$req")
  for w in "${wheels[@]}"; do
    info "wheel: ${w##*/}"
    chroot_exec python3 -m pip install --no-cache-dir --no-deps "$w"
  done

  run cp "$req" "$MNT_ROOT/tmp/requirements.all.txt"
  if ! dry "filter direct-URL lines out of requirements"; then
    grep -vE '^[A-Za-z0-9_.-]+ @ ' "$req" > "$MNT_ROOT/tmp/requirements.txt"
  fi
  chroot_exec python3 -m pip install --no-cache-dir --prefer-binary -r /tmp/requirements.txt

  info "installing pwnagotchi from the repo"
  run rm -rf "$MNT_ROOT/tmp/pwnagotchi-src"
  run rsync -a --exclude .git --exclude 'builder/work' --exclude 'builder/out' --exclude 'builder/cache' \
      "$REPO_ROOT/" "$MNT_ROOT/tmp/pwnagotchi-src/"
  chroot_exec python3 -m pip install --no-cache-dir --no-deps --no-build-isolation /tmp/pwnagotchi-src
  run rm -rf "$MNT_ROOT/tmp/pwnagotchi-src" "$MNT_ROOT/tmp/requirements.txt" "$MNT_ROOT/tmp/requirements.all.txt"
  run rm -rf "$MNT_ROOT/root/.cache"
  install_extra_bin
}

# scripts from repo bin/ that are copied as-is (not installed by pip), e.g. pwnygen
install_extra_bin() {
  local f src
  for f in ${EXTRA_BIN:-}; do
    src="$REPO_ROOT/bin/$f"
    if [[ ! -f $src ]] && (( ! DRY_RUN )); then die "EXTRA_BIN: $src not found"; fi
    run install -D -m 0755 "$src" "$MNT_ROOT/usr/local/bin/$f"
  done
}

# ---- services ----------------------------------------------------------------
stage_services() {
  step "Services"
  ensure_mounted
  local s
  for s in $ENABLE_SERVICES;  do chroot_exec systemctl enable  "$s" || warn "could not enable $s"; done
  for s in $DISABLE_SERVICES; do chroot_exec systemctl disable "$s" || warn "could not disable $s"; done
}

# ---- prune: kernels/headers/boot files for other Pi models ---------------------
stage_prune() {
  step "Prune (unused kernels, boot files)"
  if (( ! PRUNE_UNUSED_KERNELS )); then info "PRUNE_UNUSED_KERNELS=0, skipping"; return 0; fi
  ensure_mounted
  detect_kernel
  local d name
  for d in "$MNT_ROOT"/usr/lib/modules/*; do
    [[ -e $d ]] || continue
    name="${d##*/}"
    if [[ $name != "$KERNEL_RELEASE" ]]; then run rm -rf "$d"; fi
  done
  for d in "$MNT_ROOT"/usr/src/linux-headers-*; do
    [[ -e $d ]] || continue
    name="${d##*/}"
    case "$name" in
      "linux-headers-$KERNEL_RELEASE") ;;
      *-v7+|*-v7l+|*-v8+) run rm -rf "$d" ;;
    esac
  done
  run rm -f "$MNT_ROOT"/boot/kernel7.img "$MNT_ROOT"/boot/kernel7l.img "$MNT_ROOT"/boot/kernel8.img
  if ! dry "remove bcm2709/10/11/12 dtbs, start4*/fixup4* from /boot"; then
    find "$MNT_ROOT/boot" -maxdepth 1 \( -name 'bcm2709*' -o -name 'bcm2710*' -o -name 'bcm2711*' \
        -o -name 'bcm2712*' -o -name 'start4*' -o -name 'fixup4*' \) -exec rm -f {} +
  fi
  chroot_exec apt-get clean || true
}

# ---- verify ------------------------------------------------------------------
stage_verify() {
  step "Verify"
  ensure_mounted
  local missing=0 f
  for f in /usr/local/bin/pwnagotchi /usr/local/bin/pwnygen /usr/local/bin/bettercap \
           /usr/local/bin/pwngrid /usr/local/lib/libpcap.so.1 /usr/bin/pwnlib /boot/config.txt /etc/systemd/system/pwnagotchi.service \
           /usr/local/share/bettercap/caplets/pwnagotchi-auto.cap; do
    if dry "check $f"; then continue; fi
    if [[ -e $MNT_ROOT$f ]]; then ok "$f"; else warn "missing: $f"; missing=$((missing + 1)); fi
  done
  if (( missing )); then warn "$missing expected file(s) missing"; fi
  if ! dry "python3 tools/rootfs_pip_check.py --root $MNT_ROOT"; then
    python3 "$BUILDER_DIR/tools/rootfs_pip_check.py" --root "$MNT_ROOT" \
      || die "dependency check failed (see above)"
  fi
  chroot_exec python3 -c 'import pwnagotchi; print("    pwnagotchi", pwnagotchi.__version__)' || warn "import pwnagotchi failed"
}
