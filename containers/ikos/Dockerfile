# IKOS analysis container — multi-stage build
# Builder follows the steps from IKOS's own upstream CI (ubuntu:22.04/llvm-14)
# Stage 1: build IKOS from source
# Stage 2: runtime image with IKOS binary + cppcheck

FROM ubuntu:22.04 AS builder

ARG IKOS_VERSION=3.5
ENV DEBIAN_FRONTEND=noninteractive
ENV MAKEFLAGS=-j4

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    g++ \
    make \
    cmake \
    libgmp-dev \
    libboost-dev \
    libboost-filesystem-dev \
    libboost-thread-dev \
    libboost-test-dev \
    libsqlite3-dev \
    libtbb-dev \
    libz-dev \
    libedit-dev \
    python3 \
    python3-pip \
    llvm-14 \
    llvm-14-dev \
    llvm-14-tools \
    clang-14 \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth=1 --branch v${IKOS_VERSION} \
    https://github.com/NASA-SW-VnV/ikos.git /src/ikos

RUN cmake -S /src/ikos -B /build/ikos \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/ikos \
    -DLLVM_CONFIG_EXECUTABLE=/usr/lib/llvm-14/bin/llvm-config \
 && cmake --build /build/ikos \
 && cmake --install /build/ikos

# Diagnostic: show where IKOS installed Python files
RUN echo "=== /opt/ikos tree ===" \
 && find /opt/ikos -name "*.py" | head -40 \
 && echo "=== libexec/lib contents ===" \
 && ls -al /opt/ikos/libexec/lib/ 2>/dev/null || echo "(libexec/lib missing)"

# Create a stable symlink so PYTHONPATH doesn't need to know the Python version
RUN ln -sf \
    "$(find /opt/ikos/libexec/lib -maxdepth 1 -name 'python3.*' -type d | head -1)/site-packages" \
    /opt/ikos/site-packages

# ---

FROM ubuntu:22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    clang-14 \
    llvm-14 \
    llvm-14-tools \
    cppcheck \
    libsqlite3-0 \
    libgmp10 \
    libboost-filesystem1.74.0 \
    libboost-thread1.74.0 \
    libtbb12 \
    python3 \
 && rm -rf /var/lib/apt/lists/* \
 && update-alternatives --install /usr/bin/clang clang /usr/bin/clang-14 14 \
 && update-alternatives --install /usr/bin/llvm-link llvm-link /usr/bin/llvm-link-14 14

COPY --from=builder /opt/ikos /opt/ikos

ENV PATH="/opt/ikos/bin:$PATH"
ENV PYTHONPATH="/opt/ikos/site-packages"

WORKDIR /workspace
