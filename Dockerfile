#
# veripp — AI-driven formal verification for C/C++, built on ESBMC.
#
# Multi-architecture, and since ESBMC 8.6 both architectures are built the
# same way: upstream publishes a static Linux binary for x86_64
# (esbmc-linux.zip) and for arm64 (esbmc-linux-armv8.zip) on every release,
# and the image downloads the one for its platform. Until 8.6 there was no
# arm64 Linux binary anywhere but the Homebrew 8.4 bottle -- which silently
# misses out-of-bounds writes (esbmc/esbmc#6508) -- so the arm64 image used
# to compile ESBMC from source; see the git history for that recipe.
#
# Each download is checked against the digest pinned in checker/*.sha256
# before it is unpacked, and `veripp doctor` runs at build time, so an image
# whose checker cannot detect a planted bug never gets published.

# The release the binaries come from. Kept in step with ESBMC_RELEASE in
# src/veripp/checker.py and the workflows; a test checks they agree.
ARG ESBMC_VERSION=v8.6
ARG UBUNTU=24.04

# --------------------------------------------------------------- esbmc ----
FROM ubuntu:${UBUNTU} AS esbmc
ARG ESBMC_VERSION
# TARGETARCH is supplied by buildkit: amd64 | arm64.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip binutils \
    && rm -rf /var/lib/apt/lists/*
# The digests every workflow pins (see .github/workflows/ci.yml). This image
# is where the checker wheels take their binary from, so an unchecked
# download here would reach every pip install.
COPY checker/esbmc-linux.zip.sha256 checker/esbmc-linux-armv8.zip.sha256 /tmp/pins/
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) asset=esbmc-linux.zip ;; \
      arm64) asset=esbmc-linux-armv8.zip ;; \
      *) echo "no ESBMC release binary for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/esbmc.zip \
      "https://github.com/esbmc/esbmc/releases/download/${ESBMC_VERSION}/${asset}"; \
    echo "$(cat "/tmp/pins/${asset}.sha256")  /tmp/esbmc.zip" | sha256sum -c -; \
    mkdir -p /tmp/unz && unzip -q /tmp/esbmc.zip -d /tmp/unz; \
    bin="$(find /tmp/unz -name esbmc -type f | head -1)"; \
    test -n "$bin"; \
    mkdir -p /opt/esbmc/bin && cp "$bin" /opt/esbmc/bin/esbmc; \
    chmod +x /opt/esbmc/bin/esbmc; \
    rm -rf /tmp/esbmc.zip /tmp/unz /tmp/pins
# The published binaries are static and unstripped: ~700MB on x86_64, most
# of it debug symbols nobody debugging *their own C code* will ever read.
# The stripped binary still passes `veripp doctor`, which the build checks.
RUN strip --strip-unneeded /opt/esbmc/bin/esbmc && du -h /opt/esbmc/bin/esbmc
RUN test -x /opt/esbmc/bin/esbmc && /opt/esbmc/bin/esbmc --version

# ------------------------------------------------------------- runtime ----
FROM ubuntu:${UBUNTU} AS runtime

# ESBMC parses C and C++ with its own embedded clang, so it needs the system
# *headers* -- a bare python image cannot resolve <string.h> and verifies
# nothing -- but not gcc or g++ themselves. Confirmed by removing them and
# re-running both a C and a C++ verification.
# The checker is a static binary and brings its own clang resource headers,
# so nothing else is needed for it: no solver or LLVM shared objects.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv libstdc++6 libgmp10 \
        libc6-dev libstdc++-13-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=esbmc /opt/esbmc /opt/esbmc

ENV PATH="/opt/veripp/bin:/opt/esbmc/bin:${PATH}"

# veripp itself: a venv, so the image has no opinion about the system python.
COPY . /src/veripp
# --no-deps on purpose: veripp depends on veripp-checker so that a plain
# `pip install veripp` arrives with a checker, but this image has already
# built or fetched one at /opt/esbmc. Letting the wheel in too would ship the
# same checker twice and put the image ~90 MB over its size ceiling.
RUN python3 -m venv /opt/veripp \
    && /opt/veripp/bin/pip install --no-cache-dir --no-deps /src/veripp \
    && rm -rf /src/veripp /root/.cache

# Fail the build rather than ship a checker with a known blind spot.
RUN veripp doctor

# The user's code is mounted here; nothing in the image writes outside /tmp.
# The marker lets the CLI tell someone who forgot -v why their file is
# "not found", which is otherwise a true and completely unhelpful message.
ENV VERIPP_IN_CONTAINER=1
WORKDIR /src
USER 65534:65534

ENTRYPOINT ["veripp"]
CMD ["--help"]

LABEL org.opencontainers.image.title="veripp" \
      org.opencontainers.image.description="Prove C/C++ functions free of overflow, out-of-bounds, null deref and division by zero." \
      org.opencontainers.image.source="https://github.com/gfabbretti8/veripp" \
      org.opencontainers.image.licenses="Apache-2.0"
