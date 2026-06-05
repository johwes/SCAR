# IKOS analysis container — multi-stage build
# Stage 1: build IKOS from source
# Stage 2: runtime image with IKOS binary + LLVM 14 + cppcheck

FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive
ARG IKOS_VERSION=3.4

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    clang-14 \
    llvm-14-dev \
    libclang-14-dev \
    libsqlite3-dev \
    libgmp-dev \
    libboost-all-dev \
    libz-dev \
    libedit-dev \
    python3-dev \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth=1 --branch v${IKOS_VERSION} \
    https://github.com/NASA-SW-VnV/ikos.git /src/ikos

RUN cmake -S /src/ikos -B /build/ikos \
    -DCMAKE_BUILD_TYPE=Release \
    -DLLVM_CONFIG_EXECUTABLE=/usr/bin/llvm-config-14 \
    -DCMAKE_INSTALL_PREFIX=/opt/ikos \
  && cmake --build /build/ikos --parallel "$(nproc)" \
  && cmake --install /build/ikos

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
    python3 \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/clang clang /usr/bin/clang-14 14 \
 && update-alternatives --install /usr/bin/llvm-link llvm-link /usr/bin/llvm-link-14 14

COPY --from=builder /opt/ikos /opt/ikos
ENV PATH="/opt/ikos/bin:$PATH"

WORKDIR /workspace
