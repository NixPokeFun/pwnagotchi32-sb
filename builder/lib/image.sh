# shellcheck shell=bash
# ---------------------------------------------------------------------------
#  image handling: fetch, prepare/grow, mount (loop+offset, no partition
#  device nodes needed, so it also works inside Docker), chroot, finalize
# ---------------------------------------------------------------------------

# ---- partition table helpers ----------------------------------------------
# part_info N -> sets PART_START / PART_SIZE (bytes) of partition N in $IMG_PATH
part_info() {
  if (( DRY_RUN )); then PART_START=4194304; PART_SIZE=1073741824; return 0; fi
  local line
  line="$(parted -sm "$IMG_PATH" unit B print 2>/dev/null \
          | awk -F: -v n="$1" '$1==n {gsub("B","",$2); gsub("B","",$4); print $2" "$4}')"
  [[ -n $line ]] || die "cannot read partition $1 of $IMG_PATH"
  PART_START="${line% *}"
  PART_SIZE="${line#* }"
}

LOOP_DEV=""
detach_loop() {
  if [[ -n $LOOP_DEV ]]; then losetup -d "$LOOP_DEV" 2>/dev/null || true; LOOP_DEV=""; fi
}
attach_part() {   # attach_part N -> LOOP_DEV is a loop device over partition N
  part_info "$1"
  LOOP_DEV="$(losetup -f --show -o "$PART_START" --sizelimit "$PART_SIZE" "$IMG_PATH")"
  add_cleanup detach_loop
}

# ---- fetch ------------------------------------------------------------------
verify_base() {
  local expected="${BASE_IMAGE_SHA256:-}" actual
  if [[ -z $expected ]]; then
    if dry "fetch ${BASE_IMAGE_URL}.sha256 and verify"; then return 0; fi
    expected="$(curl -fsSL "${BASE_IMAGE_URL}.sha256" 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z $expected ]]; then
    warn "no checksum available for the base image, skipping verification"
    return 0
  fi
  if dry "verify sha256 $expected"; then return 0; fi
  actual="$(sha256sum "$BASE_XZ" | awk '{print $1}')"
  [[ $actual == "$expected" ]] || die "checksum mismatch for $BASE_XZ (got $actual). Delete it and retry."
  ok "base image sha256 verified"
}

fetch_base() {
  step "Fetch base image"
  info "$BASE_IMAGE_URL"
  if [[ -f $BASE_XZ ]]; then
    ok "cached: $BASE_XZ"
  else
    run curl -fL --retry 3 --retry-delay 2 -o "$BASE_XZ.part" "$BASE_IMAGE_URL"
    run mv "$BASE_XZ.part" "$BASE_XZ"
  fi
  verify_base
}

# ---- prepare: fresh working copy, grown ------------------------------------
prepare_image() {
  step "Prepare working image"
  release_mount
  info "decompress -> $IMG_PATH"
  if ! dry "xz -dc $BASE_XZ > $IMG_PATH"; then
    xz -dc -T0 "$BASE_XZ" > "$IMG_PATH.part"
    mv -f "$IMG_PATH.part" "$IMG_PATH"
  fi
  if (( EXTRA_SIZE_MB > 0 )); then
    info "growing image by ${EXTRA_SIZE_MB} MB"
    run truncate -s "+${EXTRA_SIZE_MB}M" "$IMG_PATH"
    run parted -s "$IMG_PATH" resizepart 2 100%
    if ! dry "e2fsck + resize2fs on partition 2"; then
      attach_part 2
      e2fsck -fy "$LOOP_DEV" || [[ $? -le 1 ]] || die "e2fsck failed on the root partition"
      resize2fs "$LOOP_DEV"
      detach_loop
    fi
  fi
  ok "image ready"
}

# ---- mount -------------------------------------------------------------------
is_mounted() { mountpoint -q "$MNT_ROOT"; }

# Inside a container /dev only has the loop nodes that existed at start-up
ensure_loop_nodes() {
  local i
  if (( DRY_RUN )); then return 0; fi
  [[ -e /dev/loop-control ]] || mknod -m 0660 /dev/loop-control c 10 237 2>/dev/null || true
  for i in $(seq 0 31); do
    [[ -e /dev/loop$i ]] || mknod -m 0660 "/dev/loop$i" b 7 "$i" 2>/dev/null || true
  done
}

DRY_MOUNTED=0
ensure_mounted() {
  if (( DRY_RUN && DRY_MOUNTED )); then return 0; fi
  if is_mounted; then setup_chroot; return 0; fi
  step "Mount image"
  [[ -f $IMG_PATH ]] || { if (( DRY_RUN )); then :; else die "no working image at $IMG_PATH (run the 'prepare' stage first)"; fi; }
  local rs rz bs bz
  part_info 2; rs=$PART_START; rz=$PART_SIZE
  part_info 1; bs=$PART_START; bz=$PART_SIZE
  run mkdir -p "$MNT_ROOT"
  run mount -o "loop,offset=$rs,sizelimit=$rz" "$IMG_PATH" "$MNT_ROOT"
  run mount -o "loop,offset=$bs,sizelimit=$bz" "$IMG_PATH" "$MNT_ROOT/boot"
  if (( ! KEEP_MOUNTED )); then add_cleanup release_mount; fi
  setup_chroot
  DRY_MOUNTED=1
  ok "mounted at $MNT_ROOT"
}

check_binfmt() {
  [[ -e /proc/sys/fs/binfmt_misc/qemu-arm ]] && return 0
  mountpoint -q /proc/sys/fs/binfmt_misc 2>/dev/null \
    || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null || true
  if [[ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]] && need_cmd update-binfmts; then
    update-binfmts --enable qemu-arm 2>/dev/null || true
  fi
  [[ -e /proc/sys/fs/binfmt_misc/qemu-arm ]] || die \
"qemu-arm binfmt handler is not registered (needed to run armhf binaries).
       Docker:  ./build.sh --docker   (registers it via tonistiigi/binfmt)
       Host:    apt install qemu-user-static binfmt-support"
}

setup_chroot() {
  local r=$MNT_ROOT
  if dry "chroot setup: binds, qemu-arm-static, resolv.conf, ld.so.preload, policy-rc.d"; then return 0; fi
  check_binfmt
  mountpoint -q "$r/proc"    || mount -t proc proc "$r/proc"
  mountpoint -q "$r/sys"     || mount -t sysfs sysfs "$r/sys" || warn "could not mount /sys"
  mountpoint -q "$r/dev"     || mount --bind /dev "$r/dev"
  mountpoint -q "$r/dev/pts" || mount -t devpts devpts "$r/dev/pts" || warn "could not mount /dev/pts"
  install -m 0755 "$(command -v qemu-arm-static)" "$r/usr/bin/qemu-arm-static"
  # DNS inside the chroot
  if [[ ! -e $r/etc/resolv.conf.builder && ! -e $r/etc/resolv.conf.builder.none ]]; then
    if [[ -e $r/etc/resolv.conf || -L $r/etc/resolv.conf ]]; then
      mv "$r/etc/resolv.conf" "$r/etc/resolv.conf.builder"
    else
      : > "$r/etc/resolv.conf.builder.none"
    fi
  fi
  cat /etc/resolv.conf > "$r/etc/resolv.conf"
  # libarmmem preload breaks under qemu
  if [[ -f $r/etc/ld.so.preload && ! -e $r/etc/ld.so.preload.builder ]]; then
    mv "$r/etc/ld.so.preload" "$r/etc/ld.so.preload.builder"
  fi
  # never start services while installing packages
  printf '#!/bin/sh\nexit 101\n' > "$r/usr/sbin/policy-rc.d"
  chmod 0755 "$r/usr/sbin/policy-rc.d"
}

teardown_chroot() {
  local r=$MNT_ROOT
  [[ -d $r/etc ]] || return 0
  rm -f "$r/usr/sbin/policy-rc.d" "$r/usr/bin/qemu-arm-static"
  if [[ -e $r/etc/ld.so.preload.builder ]]; then mv -f "$r/etc/ld.so.preload.builder" "$r/etc/ld.so.preload"; fi
  if [[ -e $r/etc/resolv.conf.builder ]]; then
    rm -f "$r/etc/resolv.conf"; mv "$r/etc/resolv.conf.builder" "$r/etc/resolv.conf"
  elif [[ -e $r/etc/resolv.conf.builder.none ]]; then
    rm -f "$r/etc/resolv.conf" "$r/etc/resolv.conf.builder.none"
  fi
  rm -f "$r"/tmp/builder-*.sh
}

release_mount() {
  if (( DRY_RUN )); then return 0; fi
  is_mounted || return 0
  info "unmounting image"
  teardown_chroot
  sync
  umount -R "$MNT_ROOT" 2>/dev/null || umount -R -l "$MNT_ROOT" || warn "could not unmount $MNT_ROOT"
}

# ---- chroot execution --------------------------------------------------------
chroot_exec() {   # chroot_exec CMD ARGS...
  local envs=(DEBIAN_FRONTEND=noninteractive LC_ALL=C HOME=/root)
  if [[ -n ${QEMU_CPU:-} ]]; then envs+=("QEMU_CPU=$QEMU_CPU"); fi
  run env "${envs[@]}" chroot "$MNT_ROOT" /usr/bin/env \
      PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "$@"
}

# chroot_script NAME [VAR=value ...]   (script text on stdin)
chroot_script() {
  local name=$1 dest rc=0; shift
  if dry "chroot script '$name' $*"; then cat >/dev/null; return 0; fi
  dest="$MNT_ROOT/tmp/builder-$name.sh"
  cat > "$dest"
  chmod 0755 "$dest"
  chroot_exec env "$@" "/tmp/builder-$name.sh" || rc=$?
  rm -f "$dest"
  return "$rc"
}

# ---- finalize ------------------------------------------------------------------
check_fs() {
  if dry "e2fsck -fn on the root partition"; then return 0; fi
  attach_part 2
  if e2fsck -fn "$LOOP_DEV" >/dev/null 2>&1; then ok "filesystem check clean"; else warn "e2fsck reported problems, check the image before flashing"; fi
  detach_loop
}

finalize_image() {
  step "Finalize"
  if (( ZERO_FREE_SPACE )) && is_mounted; then
    info "zero-filling free space (better compression)"
    dd if=/dev/zero of="$MNT_ROOT/zero.fill" bs=1M 2>/dev/null || true
    rm -f "$MNT_ROOT/zero.fill"; sync
  fi
  release_mount
  check_fs
  local out="$OUT_DIR/$OUT_BASENAME.img"
  case "${COMPRESS:-xz}" in
    xz)   out="$out.xz"
          if ! dry "xz -T0 -6 $IMG_PATH -> $out"; then
            xz -T0 -6 -c "$IMG_PATH" > "$out.part"; mv -f "$out.part" "$out"
          fi ;;
    none) if ! dry "copy $IMG_PATH -> $out"; then cp --sparse=always "$IMG_PATH" "$out"; fi ;;
    *)    die "COMPRESS must be 'xz' or 'none'" ;;
  esac
  if ! dry "sha256sum $(basename "$out")"; then
    ( cd "$OUT_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256" )
    if [[ -n ${HOST_UID:-} ]]; then chown "$HOST_UID:${HOST_GID:-$HOST_UID}" "$out" "$out.sha256" || true; fi
  fi
  FINAL_IMAGE="$out"
}
