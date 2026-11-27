# Build environment for the Stratolink SenseCAP M2 US915 firmware (OpenWrt 21.02, Seeed LoRa_Gateway_OpenWRT).
# The image the 2026-11-27 release was built in: ubuntu:22.04 at this digest, with these package versions.
FROM ubuntu:22.04@sha256:b000e69a50ff940390233025e7a337fb8469b509e2334fa4d018ac30f3e18cdb

ENV DEBIAN_FRONTEND=noninteractive TZ=UTC LANG=C.UTF-8
# Versions as recorded in RELEASE.json. Ubuntu's archive keeps superseded security builds only for a while; when one
# is gone, `apt-get install` fails loudly here rather than building with something else (drop the =version for that
# package, rebuild, and record the new version).
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential=12.9ubuntu3 gcc=4:11.2.0-1ubuntu1 g++=4:11.2.0-1ubuntu1 \
      python3.10 python3 python3-distutils \
      make=4.3-4.1build1 perl gawk flex bison m4 git \
      libssl-dev zlib1g-dev libncurses-dev swig rsync subversion gettext unzip wget file time ccache \
      xsltproc ca-certificates patch bzip2 xz-utils diffutils findutils \
    && rm -rf /var/lib/apt/lists/*

ARG UID=1000
RUN useradd -m -u "$UID" -s /bin/bash builder
USER builder
WORKDIR /work
