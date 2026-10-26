#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  Pwnagotchi-ai32 image builder - entry point
#
#  STATUS: official builder, still in active development.
#  Some stages are incomplete and behavior may change between versions.
#
#  Usage:  ./build.sh [options] [command]
#
#  Commands:
#    build (default)   run all stages: fetch -> ... -> compress
#    mount             mount the working image (native mode only)
#    umount            unmount it again
#    shell             open a shell inside the mounted image (qemu)
#    clean             remove the working image (cache and out are kept)
#    stages            list stage names
#
#  Options:
#    --docker | --native   where to run (default: auto)
#    --config FILE         extra config file
#    --work-dir DIR  --out-dir DIR  --cache-dir DIR
#    --only a,b            run only these stages   (see 'stages')
#    --skip a,b            skip these stages
#    --no-compress         keep the finished image as .img
#    --keep-mounted        do not unmount on exit
#    --no-binfmt           (docker) do not register the qemu handler
#    --dry-run             print what would be done
#    --accept-risk         skip the confirmation prompt
#    -h | --help
# ---------------------------------------------------------------------------
set -Eeuo pipefail

BUILDER_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$BUILDER_DIR/.." && pwd)"

# shellcheck source=lib/common.sh
. "$BUILDER_DIR/lib/common.sh"
# shellcheck source=lib/image.sh
. "$BUILDER_DIR/lib/image.sh"
# shellcheck source=lib/stages.sh
. "$BUILDER_DIR/lib/stages.sh"
# shellcheck source=lib/docker.sh
. "$BUILDER_DIR/lib/docker.sh"

COMMAND=build
MODE=auto
ACCEPT_RISK=0
KEEP_MOUNTED=0
NO_BINFMT=0
ONLY_STAGES=""
SKIP_STAGES=""
FWD_ARGS=()
FINAL_IMAGE=""

trap 'printf "\n"; fail "Interrupted."; exit 130' INT TERM
trap run_cleanup EXIT

usage() { sed -n '/^#  Usage:/,/^#    -h | --help/p' "${BASH_SOURCE[0]}" | sed -e 's/^#  \{0,1\}//' -e 's/^#$//'; }

banner() {
  printf '\n  %s+-------------------------------+%s\n' "$CYN" "$RST"
  printf '  %s|%s P W N A G O T C H I - A I 3 2 %s|%s\n' "$CYN" "$BLD" "$CYN" "$RST"
  printf '  %s+-------------------------------+%s\n' "$CYN" "$RST"
  printf '  %sImage builder%s  %s|  in development%s\n\n' "$BLD" "$RST" "$DIM" "$RST"
}

notice() {
  box_top  "$YEL"
  box_line "$YEL" "$BLD"  "  NOTICE: THE BUILDER IS STILL IN DEVELOPMENT"
  box_top  "$YEL"
  box_line "$YEL" ""      ""
  box_line "$YEL" ""      "  This is the official Pwnagotchi-ai32 image builder, but"
  box_line "$YEL" ""      "  it is not finished yet. It will grow into a full builder"
  box_line "$YEL" ""      "  over time."
  box_line "$YEL" ""      ""
  box_line "$YEL" ""      "  What that means right now:"
  box_line "$YEL" ""      "   - some build stages are missing or incomplete"
  box_line "$YEL" ""      "   - behavior and options may change between versions"
  box_line "$YEL" ""      "   - rough edges are expected, some messages may be off"
  box_line "$YEL" ""      ""
  box_line "$YEL" ""      "  Tip: use a disposable machine/VM and keep backups of any"
  box_line "$YEL" ""      "  SD card or image you care about. Found a problem? Please"
  box_line "$YEL" ""      "  open an issue on GitHub."
  box_line "$YEL" ""      ""
  box_end  "$YEL"
  printf '\n'
}

# ---- argument parsing --------------------------------------------------------
while (( $# )); do
  case "$1" in
    build|mount|umount|shell|clean|stages) COMMAND=$1 ;;
    --docker)        MODE=docker ;;
    --native)        MODE=native ;;
    --config)        CLI_CONFIG=${2:?--config needs a file}; FWD_ARGS+=(--config "$2"); shift ;;
    --work-dir)      CLI_WORK_DIR=${2:?--work-dir needs a directory}; shift ;;
    --out-dir)       CLI_OUT_DIR=${2:?--out-dir needs a directory}; shift ;;
    --cache-dir)     CLI_CACHE_DIR=${2:?--cache-dir needs a directory}; shift ;;
    --only)          ONLY_STAGES=${2:?--only needs a list}; FWD_ARGS+=(--only "$2"); shift ;;
    --skip)          SKIP_STAGES=${2:?--skip needs a list}; FWD_ARGS+=(--skip "$2"); shift ;;
    --no-compress)   CLI_NO_COMPRESS=1; FWD_ARGS+=(--no-compress) ;;
    --keep-mounted)  KEEP_MOUNTED=1; FWD_ARGS+=(--keep-mounted) ;;
    --no-binfmt)     NO_BINFMT=1 ;;
    --dry-run)       DRY_RUN=1; FWD_ARGS+=(--dry-run) ;;
    --accept-risk)   ACCEPT_RISK=1 ;;
    -h|--help)       usage; exit 0 ;;
    *) fail "Unknown option: $1"; usage; exit 2 ;;
  esac
  shift
done

# ---- preflight -------------------------------------------------------------------
REQUIRED_DIRS=(assets brcm data lib tools)
check_layout() {
  local missing=0 d
  info "Checking builder layout in ${DIM}${BUILDER_DIR}${RST}"
  for d in "${REQUIRED_DIRS[@]}"; do
    if [[ -d "$BUILDER_DIR/$d" ]]; then ok "$d/ found"; else fail "$d/ is missing"; missing=1; fi
  done
  [[ -f $REPO_ROOT/requirements.txt ]] || { fail "requirements.txt not found in $REPO_ROOT"; missing=1; }
  if (( missing )); then die "Builder folder is incomplete. Re-clone the repository and try again."; fi
}

NATIVE_TOOLS=(losetup parted e2fsck resize2fs mount umount chroot xz curl sha256sum rsync depmod
              qemu-arm-static openssl unzip python3 truncate mountpoint)
missing_tools() {
  local t m=()
  for t in "${NATIVE_TOOLS[@]}"; do need_cmd "$t" || m+=("$t"); done
  printf '%s ' ${m[@]+"${m[@]}"}
}

choose_mode() {
  if in_container; then MODE=native; return; fi
  if [[ $MODE == auto ]]; then
    if [[ $EUID -eq 0 && -z "$(missing_tools)" ]]; then MODE=native
    elif docker_ok; then MODE=docker
    else MODE=native; fi
  fi
}

check_env() {
  local arch os avail miss
  arch="$(uname -m)"
  os="$( (. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-unknown}") || printf 'unknown')"
  avail="$(df -h --output=avail "$BUILDER_DIR" 2>/dev/null | tail -n1 | tr -d ' ' || true)"
  printf '\n'
  info "Environment"
  printf '       mode      : %s\n' "$MODE"
  printf '       arch / os : %s / %s\n' "$arch" "$os"
  printf '       free space: %s\n' "${avail:-unknown}"
  printf '       base image: %s\n' "${BASE_IMAGE_URL##*/}"
  printf '       work dir  : %s\n' "$WORK_DIR"
  printf '       out dir   : %s\n' "$OUT_DIR"
  printf '       cache dir : %s\n' "$CACHE_DIR"
  printf '\n'
  if [[ $MODE == native ]]; then
    miss="$(missing_tools)"
    if [[ -n $miss ]]; then
      warn "Some optional tools might be missing or not in PATH. This may adversely affect the final image later."
    fi
    if (( EUID != 0 )) && ! in_container; then
      die "Native mode needs root privileges (loop devices, mounts). Please use sudo or pass --docker."
    fi
  else
    docker_ok || (( DRY_RUN )) || die "Docker mode selected but docker is not usable (Docker Desktop running? WSL integration enabled for this distro?)."
  fi
}

confirm() {
  if (( ACCEPT_RISK || DRY_RUN )); then return 0; fi
  if [[ ! -t 0 ]]; then die "Non-interactive shell. Re-run with --accept-risk if you really mean it."; fi
  local answer
  printf '%sProceed with build? [y/N]:%s ' "$BLD" "$RST"
  read -r answer
  case "${answer,,}" in
    y|yes) printf '\n' ;;
    *) printf '\n'; info "Aborted. Nothing was changed."; exit 0 ;;
  esac
}

# ---- commands ------------------------------------------------------------------------
run_pipeline() {
  local s
  for s in "${ALL_STAGES[@]}"; do
    if ! stage_enabled "$s"; then info "skip stage: $s"; continue; fi
    case "$s" in
      fetch)     fetch_base ;;
      prepare)   prepare_image ;;
      system)    stage_system ;;
      apt)       stage_apt ;;
      firmware)  stage_firmware ;;
      files)     stage_files ;;
      artifacts) stage_artifacts ;;
      python)    stage_python ;;
      services)  stage_services ;;
      prune)     stage_prune ;;
      verify)    stage_verify ;;
      compress)  finalize_image ;;
    esac
  done
  if [[ -n $FINAL_IMAGE ]]; then
    printf '\n'; ok "Done: $FINAL_IMAGE"
    info "Flash it with Raspberry Pi Imager (\"Use custom\") or balenaEtcher."
  fi
}

cmd_clean() {
  release_mount
  run rm -f "$IMG_PATH" "$IMG_PATH.part"
  ok "working image removed (cache and out are kept)"
}

main() {
  case "$COMMAND" in
    stages) printf '%s\n' "${ALL_STAGES[@]}"; exit 0 ;;
  esac

  load_config
  resolve_paths
  banner
  notice
  check_layout
  choose_mode

  # inside Docker the real work happens in the container
  if [[ $MODE == docker && $COMMAND != build && $COMMAND != shell ]]; then
    die "'$COMMAND' needs native mode (mounts are not visible outside the container)."
  fi
  check_env

  if [[ $MODE == docker ]]; then
    confirm
    docker_run_builder
    return 0
  fi

  make_dirs
  if in_container; then ensure_loop_nodes; fi
  case "$COMMAND" in
    build)  confirm; run_pipeline ;;
    mount)  KEEP_MOUNTED=1; ensure_mounted; ok "mounted at $MNT_ROOT (unmount with: ./build.sh umount)" ;;
    umount) release_mount; ok "unmounted" ;;
    shell)  ensure_mounted; info "entering chroot, exit with Ctrl-D"
            QEMU_CPU="${QEMU_CPU:-}" chroot "$MNT_ROOT" /bin/bash -l || true ;;
    clean)  cmd_clean ;;
  esac
}

main
