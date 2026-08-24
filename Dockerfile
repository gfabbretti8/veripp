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
#                source. Configured as below -- without the frontends and test
#                suites we do not ship -- that compile is about 3.5 minutes on
#                four native arm64 cores, not the hour a full build.sh run
#                costs. It is the only way to hand arm64 users a checker as
#                sound as the one amd64 users get.
#
# Both paths land the same layout at /opt/esbmc/bin/esbmc, and `veripp doctor`
# runs at build time on both, so an image that cannot detect a planted bug
# never gets published.

# The prebuilt amd64 asset comes from a release tag.
ARG ESBMC_VERSION=weekly
# The arm64 source build takes a git ref, and it is deliberately NOT `weekly`.
# The weekly tag is described as rolling but is in practice cut infrequently --
# at the time of writing it points at 2026-05-27, with master ~1900 commits
# ahead. esbmc/esbmc#5252, which is what makes an arm64 Linux build possible at
# all (SVE builtin types, 32-bit libc, Solidity stub), merged 2026-06-09, two
# weeks after that tag. Building `weekly` on arm64 therefore fails on the very
# bug whose fix is already upstream.
ARG ESBMC_SOURCE_REF=master
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
    mkdir -p /opt/esbmc/bin /opt/esbmc/lib && cp "$bin" /opt/esbmc/bin/esbmc; \
    chmod +x /opt/esbmc/bin/esbmc; \
    rm -rf /tmp/esbmc.zip /tmp/unz

# ---------------------------------------------------------------- arm64 ----
# Mirrors esbmc's own .github/workflows/release.yml `build-linux-arm64` job,
# which is the only arm64 Linux build recipe the project actually exercises.
FROM ubuntu:${UBUNTU} AS esbmc-arm64
ARG ESBMC_SOURCE_REF
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git wget gnupg unzip \
        build-essential cmake ninja-build python3 python3-dev \
        bison flex libboost-all-dev libgmp-dev libssl-dev libz3-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/llvm.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/llvm.gpg] http://apt.llvm.org/noble/ llvm-toolchain-noble-22 main" \
      > /etc/apt/sources.list.d/llvm.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      llvm-22 llvm-22-dev clang-22 libclang-22-dev libclang-cpp22-dev; \
    rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${ESBMC_SOURCE_REF}" \
      https://github.com/esbmc/esbmc.git /src/esbmc
WORKDIR /src/esbmc

# Configured directly rather than through ./scripts/build.sh. That script
# targets the project's own CI and turns on the regression suite, csmith, and
# the Solidity/Jimple/Python frontends, none of which we ship -- and on arm64
# two of its defaults are fatal:
#
#   ENABLE_BUNDLE_LIBC_32BIT  builds a 32-bit libc model, which needs 32-bit
#                             headers that do not exist on arm64 (build.sh
#                             itself skips g++-multilib there, then asks for
#                             the model anyway). Homebrew's formula turns this
#                             off on Linux for the same reason.
#   ENABLE_PYTHON_FRONTEND    pulls library/python/*.c into the libc model.
#
# These flags mirror Homebrew's esbmc formula, which is the configuration
# known to produce a working arm64 Linux build.
RUN set -eux; \
    cmake -S . -B build -GNinja \
      -DDOWNLOAD_DEPENDENCIES=On \
      -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
      -DENABLE_BUNDLE_LIBC_32BIT=OFF \
      -DLLVM_DIR=/usr/lib/llvm-22/lib/cmake/llvm \
      -DClang_DIR=/usr/lib/cmake/clang-22 \
      -DENABLE_Z3=ON -DZ3_DIR=/usr \
      -DENABLE_BOOLECTOR=OFF \
      -DENABLE_BITWUZLA=OFF \
      -DENABLE_GOTO_CONTRACTOR=OFF \
      -DENABLE_PYTHON_FRONTEND=OFF \
      -DENABLE_SOLIDITY_FRONTEND=OFF \
      -DENABLE_JIMPLE_FRONTEND=OFF \
      -DENABLE_CSMITH=OFF \
      -DENABLE_FUZZER=OFF \
      -DBUILD_TESTING=OFF \
      -DENABLE_REGRESSION=OFF \
      -DBUILD_STATIC=OFF \
      -DENABLE_WERROR=OFF \
      -DCMAKE_INSTALL_PREFIX=/opt/esbmc; \
    cmake --build build; \
    cmake --install build
# BUILD_STATIC=OFF (as Homebrew does) leaves esbmc linked against LLVM and
# clang shared objects that come from apt.llvm.org, not stock Ubuntu. Rather
# than add that repository to the runtime image -- which the amd64 half, a
# static binary, has no use for -- bundle exactly what ldd asks for that the
# runtime will not already have, and let each architecture hand the runtime a
# self-contained /opt/esbmc tree.
RUN set -eux; \
    mkdir -p /opt/esbmc/lib; \
    for lib in $(ldd /opt/esbmc/bin/esbmc | awk '/=>/ {print $3}'); do \
      case "$lib" in \
        */libLLVM*|*/libclang*) cp -L "$lib" /opt/esbmc/lib/ ;; \
      esac; \
    done; \
    ls -1 /opt/esbmc/lib

RUN test -x /opt/esbmc/bin/esbmc \
    && LD_LIBRARY_PATH=/opt/esbmc/lib /opt/esbmc/bin/esbmc --version

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
        libz3-4 libedit2 libxml2 libzstd1 liblzma5 libtinfo6 libffi8 \
        libboost-filesystem1.83.0 libboost-program-options1.83.0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=esbmc /opt/esbmc /opt/esbmc
ENV PATH="/opt/veripp/bin:/opt/esbmc/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/esbmc/lib"

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
