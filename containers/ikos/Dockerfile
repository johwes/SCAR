# IKOS analysis container
# Provides: LLVM 14, clang, llvm-link, NASA IKOS, cppcheck
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    clang-14 \
    llvm-14 \
    llvm-14-tools \
    cppcheck \
    cmake \
    libsqlite3-dev \
    libgmp-dev \
    libboost-dev \
    python3 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Symlink LLVM 14 tools to PATH
RUN update-alternatives --install /usr/bin/clang clang /usr/bin/clang-14 14 \
 && update-alternatives --install /usr/bin/llvm-link llvm-link /usr/bin/llvm-link-14 14

# TODO: build IKOS from source or install from package
# https://github.com/NASA-SW-VnV/ikos
# RUN <ikos build steps>

WORKDIR /workspace
