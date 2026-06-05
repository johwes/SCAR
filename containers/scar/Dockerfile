# SCAR agent container
# Provides: Python 3.11, scar package, ripgrep, patch
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ripgrep \
    patch \
    clang \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY scar/ scar/

RUN pip install --no-cache-dir -e .

WORKDIR /workspace
ENTRYPOINT ["python", "-m", "scar"]
