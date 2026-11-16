# Test environment only. Production runs directly on the VM under systemd.
ARG BASE_IMAGE=debian:13-slim
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv python3-libtorrent ca-certificates systemd \
    && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN python3 -m venv --system-site-packages /opt/testenv \
    && /opt/testenv/bin/pip install -c /src/deploy/linux/constraints.txt '/src[server,dev]'
RUN mkdir -p /opt/infocon-librarian /mnt/infocon \
    && ln -s /opt/testenv /opt/infocon-librarian/current \
    && useradd --system --user-group infocon \
    && systemd-analyze verify /src/deploy/linux/infocon-librarian.service
WORKDIR /src
ENV PATH="/opt/testenv/bin:$PATH"
CMD ["python", "-m", "pytest", "tests/unit", "tests/integration", "tests/engine", "-q"]
