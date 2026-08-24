#
# veripp — AI-driven formal verification for C/C++, built on ESBMC.
#
# Multi-architecture by construction, but the two architectures are NOT
# built the same way, and the reason matters:
#
#   linux/amd64  ESBMC publishes a prebuilt x86_64 binary on every `weekly`
#                tag. We download it. Fast.
#   linux/arm64  ESBMC publishes no arm64 Linux binary. The only prebuilt
#                arm64 ESBMC anywhere is the Homebrew bottle, and that is
#                pinned to the 8.4 release -- which silently misses
#                out-of-bounds writes (esbmc/esbmc#6508) and is therefore
#                unsound for our purposes. So on arm64 we build ESBMC from
#                source. It is slow, it happens once per release, and it is
#                the only way to hand arm64 users a checker that is as sound
#                as the one amd64 users get.
#
# Both paths land the same layout at /opt/esbmc/bin/esbmc, and `veripp doctor`
# runs at build time on both, so an image that cannot detect a planted bug
# never gets published.

ARG ESBMC_VERSION=weekly
ARG UBUNTU=24.04

# ---------------------------------------------------------------- amd64 ----
FROM ubuntu:${UBUNTU} AS esbmc-amd64
ARG ESBMC_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    curl -fsSL -o /tmp/esbmc.zip \
      "https://github.com/esbmc/esbmc/releases/download/${ESBMC_VERSION}/esbmc-linux.zip"; \
    mkdir -p /tmp/unz && unzip -q /tmp/esbmc.zip -d /tmp/unz; \
    bin="$(find /tmp/unz -name esbmc -type f | head -1)"; \
    test -n "$bin"; \
    mkdir -p /opt/esbmc/bin && cp "$bin" /opt/esbmc/bin/esbmc; \
    chmod +x /opt/esbmc/bin/esbmc; \
    rm -rf /tmp/esbmc.zip /tmp/unz

# ---------------------------------------------------------------- arm64 ----
# Mirrors esbmc's own .github/workflows/release.yml `build-linux-arm64` job,
# which is the only arm64 Linux build recipe the project actually exercises.
FROM ubuntu:${UBUNTU} AS esbmc-arm64
ARG ESBMC_VERSION
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git wget gnupg unzip \
        build-essential cmake ninja-build python3 python3-pip python3-venv \
        bison flex libboost-all-dev libgmp-dev libssl-dev \
        ccache pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/llvm.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/llvm.gpg] http://apt.llvm.org/noble/ llvm-toolchain-noble-22 main" \
      > /etc/apt/sources.list.d/llvm.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      llvm-22 clang-22 libclang-22-dev libclang-cpp22-dev; \
    rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    git clone --depth 1 --branch "${ESBMC_VERSION}" \
      https://github.com/esbmc/esbmc.git /src/esbmc \
    || git clone --depth 1 https://github.com/esbmc/esbmc.git /src/esbmc
WORKDIR /src/esbmc
RUN ./scripts/build.sh -b Release -S OFF -c 22 -e OFF
RUN set -eux; \
    bin="$(find /src/esbmc -name esbmc -type f -perm -u+x | head -1)"; \
    test -n "$bin"; \
    mkdir -p /opt/esbmc/bin && cp "$bin" /opt/esbmc/bin/esbmc

# ------------------------------------------------------------ selector ----
# TARGETARCH is supplied by buildkit: amd64 | arm64.
ARG TARGETARCH
FROM esbmc-${TARGETARCH} AS esbmc

# ------------------------------------------------------------- runtime ----
FROM ubuntu:${UBUNTU} AS runtime

# ESBMC shells out to a C/C++ preprocessor and needs system headers to parse
# real code; a bare python image cannot verify anything.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv libstdc++6 libgomp1 libgmp10 \
        gcc g++ libc6-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=esbmc /opt/esbmc/bin/esbmc /opt/esbmc/bin/esbmc
ENV PATH="/opt/veripp/bin:/opt/esbmc/bin:${PATH}"

# veripp itself: a venv, so the image has no opinion about the system python.
COPY . /src/veripp
RUN python3 -m venv /opt/veripp \
    && /opt/veripp/bin/pip install --no-cache-dir /src/veripp \
    && rm -rf /src/veripp /root/.cache

# Fail the build rather than ship a checker with a known blind spot.
RUN veripp doctor

# The user's code is mounted here; nothing in the image writes outside /tmp.
WORKDIR /src
USER 65534:65534

ENTRYPOINT ["veripp"]
CMD ["--help"]

LABEL org.opencontainers.image.title="veripp" \
      org.opencontainers.image.description="Prove C/C++ functions free of overflow, out-of-bounds, null deref and division by zero." \
      org.opencontainers.image.source="https://github.com/gfabbretti8/veripp" \
      org.opencontainers.image.licenses="Apache-2.0"
