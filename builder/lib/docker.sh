# shellcheck shell=bash
# ---------------------------------------------------------------------------
#  Docker wrapper: runs this same build.sh (--native) inside a privileged
#  Debian bookworm container with qemu-user-static. Works from WSL2 with
#  Docker Desktop (WSL integration enabled).
# ---------------------------------------------------------------------------

in_container() { [[ -f /.dockerenv ]]; }

# 'docker' may exist as a stub (WSL without Docker Desktop integration): ask the daemon
docker_ok() { need_cmd docker && docker info >/dev/null 2>&1; }

docker_run_builder() {
  if ! docker_ok && (( ! DRY_RUN )); then
    die "docker is not usable here (Docker Desktop running? Settings > Resources > WSL integration enabled for this distro?)"
  fi
  local image="${DOCKER_IMAGE:-pwnagotchi-ai32-builder}" tty=()

  step "Docker: builder image ($image)"
  # Dockerfile is sent on stdin: no build context, so work/ and cache/ are never uploaded
  if ! dry "docker build -t $image - < Dockerfile"; then
    docker build -t "$image" - < "$BUILDER_DIR/Dockerfile"
  fi

  if (( ! NO_BINFMT )); then
    info "registering the armhf qemu handler on the host kernel (idempotent)"
    run docker run --privileged --rm tonistiigi/binfmt --install arm
  fi

  run mkdir -p "$WORK_DIR" "$OUT_DIR" "$CACHE_DIR"
  if [[ -t 0 && -t 1 ]]; then tty=(-it); fi

  step "Docker: running the build"
  run docker run --rm ${tty[@]+"${tty[@]}"} --privileged \
      -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
      -v "$REPO_ROOT:/repo" -v "$WORK_DIR:/work" -v "$OUT_DIR:/out" -v "$CACHE_DIR:/cache" \
      -w /repo/builder "$image" \
      bash ./build.sh --native --accept-risk \
        --work-dir /work --out-dir /out --cache-dir /cache \
        ${FWD_ARGS[@]+"${FWD_ARGS[@]}"} "$COMMAND"
}
