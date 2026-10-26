# shellcheck shell=bash
# ---------------------------------------------------------------------------
#  common helpers: colors, logging, dry-run wrapper, cleanup stack, config
# ---------------------------------------------------------------------------

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  RED=$'\e[1;31m'; YEL=$'\e[1;33m'; GRN=$'\e[1;32m'; CYN=$'\e[1;36m'
  DIM=$'\e[2m'; BLD=$'\e[1m'; RST=$'\e[0m'
else
  RED=''; YEL=''; GRN=''; CYN=''; DIM=''; BLD=''; RST=''
fi
W=64

hr()       { printf '%*s' "$W" '' | tr ' ' "$1"; }
box_top()  { printf '%s+%s+%s\n' "$1" "$(hr '-')" "$RST"; }
box_line() { printf '%s|%s %-*s %s|%s\n' "$1" "$RST$2" $((W - 2)) "$3" "$1" "$RST"; }
box_end()  { box_top "$1"; }
info()     { printf '%s[ .. ]%s %s\n' "$CYN" "$RST" "$*"; }
ok()       { printf '%s[ OK ]%s %s\n' "$GRN" "$RST" "$*"; }
warn()     { printf '%s[WARN]%s %s\n' "$YEL" "$RST" "$*"; }
fail()     { printf '%s[FAIL]%s %s\n' "$RED" "$RST" "$*" >&2; }
die()      { fail "$*"; exit 1; }
step()     { printf '\n%s==>%s %s%s%s\n' "$CYN" "$RST" "$BLD" "$*" "$RST"; }
need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---- dry-run support -------------------------------------------------------
DRY_RUN=0

# dry "what would happen": prints and returns 0 in dry-run mode, 1 otherwise.
#   if dry "do X"; then :; else do_x; fi
dry() {
  if (( DRY_RUN )); then
    printf '%s  [dry-run]%s %s\n' "$DIM" "$RST" "$*"
    return 0
  fi
  return 1
}

# run CMD...: execute, or only print in dry-run mode
run() {
  if (( DRY_RUN )); then
    printf '%s  +%s' "$DIM" "$RST"
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

# ---- cleanup stack (LIFO), executed on EXIT --------------------------------
CLEANUP_FUNCS=()
add_cleanup() { CLEANUP_FUNCS=("$1" ${CLEANUP_FUNCS[@]+"${CLEANUP_FUNCS[@]}"}); }
run_cleanup() {
  local f fs=()
  fs=(${CLEANUP_FUNCS[@]+"${CLEANUP_FUNCS[@]}"})
  CLEANUP_FUNCS=()
  for f in ${fs[@]+"${fs[@]}"}; do "$f" || true; done
}

# ---- config ----------------------------------------------------------------
# Order: config.env -> config.local.env -> --config FILE -> CLI flags
load_config() {
  local f
  for f in "$BUILDER_DIR/config.env" "$BUILDER_DIR/config.local.env" "${CLI_CONFIG:-}"; do
    [[ -n $f ]] || continue
    if [[ -f $f ]]; then
      # shellcheck disable=SC1090
      . "$f"
    elif [[ $f == "${CLI_CONFIG:-}" ]]; then
      die "config file not found: $f"
    fi
  done
  if [[ -n ${CLI_WORK_DIR:-}  ]]; then WORK_DIR=$CLI_WORK_DIR; fi
  if [[ -n ${CLI_OUT_DIR:-}   ]]; then OUT_DIR=$CLI_OUT_DIR; fi
  if [[ -n ${CLI_CACHE_DIR:-} ]]; then CACHE_DIR=$CLI_CACHE_DIR; fi
  if [[ -n ${CLI_NO_COMPRESS:-} ]]; then COMPRESS=none; fi
}

detect_version() {
  local v=""
  if [[ -f $REPO_ROOT/pwnagotchi/_version.py ]]; then
    v="$(sed -nE "s/^__version__[[:space:]]*=[[:space:]]*['\"]([^'\"]+).*/\1/p" \
         "$REPO_ROOT/pwnagotchi/_version.py" | head -n1)"
  fi
  printf '%s' "${v:-dev}"
}

# Directories used by the builder (all overridable):
#   CACHE_DIR  downloaded base image + artifacts (kept between builds)
#   WORK_DIR   working image + mount points (disposable)
#   OUT_DIR    finished images
resolve_paths() {
  : "${WORK_DIR:=$BUILDER_DIR/work}" "${OUT_DIR:=$BUILDER_DIR/out}" "${CACHE_DIR:=$BUILDER_DIR/cache}"
  WORK_DIR="$(realpath -m "$WORK_DIR")"
  OUT_DIR="$(realpath -m "$OUT_DIR")"
  CACHE_DIR="$(realpath -m "$CACHE_DIR")"
  MNT_ROOT="$WORK_DIR/mnt/root"
  IMG_PATH="$WORK_DIR/${IMAGE_NAME}.img"
  BASE_XZ="$CACHE_DIR/${BASE_IMAGE_URL##*/}"
  [[ -n ${IMAGE_VERSION:-} ]] || IMAGE_VERSION="$(detect_version)"
  OUT_BASENAME="${IMAGE_NAME}-${IMAGE_VERSION}"
  if [[ $WORK_DIR == /mnt/[a-z]/* ]]; then
    warn "WORK_DIR is on a Windows drive ($WORK_DIR): slow, and loop mounts can misbehave."
    warn "Prefer a Linux path, e.g.  --work-dir \$HOME/pwn-work"
  fi
}

make_dirs() {
  run mkdir -p "$WORK_DIR" "$OUT_DIR" "$CACHE_DIR" "$MNT_ROOT"
}

write_file() {   # write_file PATH CONTENT
  if dry "write $1"; then return 0; fi
  printf '%s\n' "$2" > "$1"
}
