# SCAR agent container
# Provides: Python 3.11, scar package, ripgrep, patch

FROM quay.io/centos/centos:stream9

RUN dnf install -y dnf-plugins-core epel-release \
 && dnf install -y \
    python3 \
    python3-pip \
    ripgrep \
    patch \
    clang \
 && dnf clean all

WORKDIR /app
COPY pyproject.toml .
COPY scar/ scar/

RUN pip3 install --no-cache-dir -e .

WORKDIR /workspace
ENTRYPOINT ["python3", "-m", "scar"]
