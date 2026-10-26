# Build container for the pwnagotchi-ai32 image builder.
#
# The TARGET system is Raspberry Pi OS "bullseye" (32-bit, armv6 compatible).
# The build container itself does not have to be bullseye, and should not be:
# Debian 11 reached end of life on 31 Aug 2026 and its packages moved to
# archive.debian.org, so `apt-get install` in a bullseye container is fragile.
# bookworm is still supported and ships a newer qemu-user-static.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl xz-utils unzip \
      util-linux parted e2fsprogs dosfstools kmod rsync \
      qemu-user-static binfmt-support \
      openssl python3 python3-packaging \
      coreutils findutils grep gawk \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /repo/builder
