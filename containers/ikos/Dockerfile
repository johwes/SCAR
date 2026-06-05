# IKOS analysis container — multi-stage build
# Stage 1: build IKOS from source
# Stage 2: runtime image with IKOS binary + LLVM 14 + cppcheck

FROM quay.io/centos/centos:stream9 AS builder

ARG IKOS_VERSION=3.5

RUN dnf install -y dnf-plugins-core \
 && dnf config-manager --set-enabled crb \
 && dnf install -y \
    git \
    cmake \
    make \
    gcc \
    gcc-c++ \
    clang \
    llvm-devel \
    clang-devel \
    sqlite-devel \
    gmp-devel \
    boost-devel \
    zlib-devel \
    libedit-devel \
    python3-devel \
    tbb-devel \
 && dnf clean all

# tbb-devel on CentOS Stream 9 installs headers to /usr/include/tbb/ but
# IKOS's FindTBB.cmake looks for the oneTBB layout at /usr/include/oneapi/tbb/
RUN mkdir -p /usr/include/oneapi \
 && ln -sf /usr/include/tbb /usr/include/oneapi/tbb

RUN git clone --depth=1 --branch v${IKOS_VERSION} \
    https://github.com/NASA-SW-VnV/ikos.git /src/ikos

RUN cmake -S /src/ikos -B /build/ikos \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/ikos \
    -DTBB_ROOT=/usr \
  && cmake --build /build/ikos --parallel "$(nproc)" \
  && cmake --install /build/ikos

# ---

FROM quay.io/centos/centos:stream9 AS runtime

RUN dnf install -y dnf-plugins-core \
 && dnf config-manager --set-enabled crb \
 && dnf install -y \
    clang \
    llvm \
    cppcheck \
    sqlite-libs \
    gmp \
    boost-filesystem \
    boost-thread \
    tbb \
    python3 \
 && dnf clean all

RUN update-alternatives --install /usr/bin/llvm-link llvm-link \
    /usr/bin/llvm-link-14 14 2>/dev/null || true

COPY --from=builder /opt/ikos /opt/ikos
ENV PATH="/opt/ikos/bin:$PATH"

WORKDIR /workspace
