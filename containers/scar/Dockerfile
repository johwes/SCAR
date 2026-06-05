# SCAR agent container
# Provides: Python 3.10, scar package, ripgrep, patch, clang

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ripgrep \
    patch \
    clang-14 \
 && rm -rf /var/lib/apt/lists/* \
 && update-alternatives --install /usr/bin/clang clang /usr/bin/clang-14 14

WORKDIR /app
COPY pyproject.toml .
COPY scar/ scar/

RUN pip3 install --no-cache-dir -e .

WORKDIR /workspace
ENTRYPOINT ["python3", "-m", "scar"]
