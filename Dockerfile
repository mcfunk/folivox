# Folivox studio in a container: `folivox ui` on 0.0.0.0:8765, everything it keeps under /data.
# See docs/docker.md (Docker, Compose and Swarm).
#
#   docker build -t folivox .                                         NVIDIA GPU (PyTorch 2.5, CUDA 12.4)
#   docker build -t folivox --build-arg TORCH=cpu .                   no GPU (smaller, slow synthesis)
#   docker build -t folivox --build-arg TORCH=cu128 .                 RTX 50 series (PyTorch 2.7, CUDA 12.8)
#
# The GPU build needs no CUDA base image: the PyTorch wheels carry CUDA, the host only provides the driver
# (NVIDIA Container Toolkit).
FROM python:3.10-slim-bookworm

ARG TORCH=cuda
ARG UID=1000

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libgomp1 tini \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.8.22

ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never VIRTUAL_ENV=/opt/folivox
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
# Same PyTorch choice as scripts/install.sh: the tested <2.6 everywhere but RTX 50 cards.
RUN set -eu; \
    case "$TORCH" in \
      cuda)  printf 'torch<2.6\ntorchaudio<2.6\n' > /tmp/torch.txt; EXTRA="" ;; \
      cpu)   printf 'torch<2.6\ntorchaudio<2.6\n' > /tmp/torch.txt; EXTRA="https://download.pytorch.org/whl/cpu" ;; \
      cu128) printf 'torch==2.7.1\ntorchaudio==2.7.1\n' > /tmp/torch.txt; EXTRA="https://download.pytorch.org/whl/cu128" ;; \
      *) echo "TORCH must be cuda, cpu or cu128" >&2; exit 1 ;; \
    esac; \
    uv venv "$VIRTUAL_ENV" --python /usr/local/bin/python3.10; \
    uv pip install --no-cache --constraints /tmp/torch.txt \
      ${EXTRA:+--index-strategy unsafe-best-match --extra-index-url "$EXTRA"} ".[all]"; \
    rm -rf /tmp/torch.txt /src

COPY deploy/entrypoint.sh /usr/local/bin/folivox-entrypoint
RUN chmod 755 /usr/local/bin/folivox-entrypoint \
 && useradd --uid "$UID" --home-dir /data --create-home --shell /usr/sbin/nologin folivox \
 && mkdir -p /data/Audiobooks && chown -R folivox:folivox /data

# HOME=/data: models, voices, studio state and the config (platformdirs) plus the Hugging Face cache all land in
# the volume, and the studio's default output folder is /data/Audiobooks.
ENV PATH=/opt/folivox/bin:$PATH \
    HOME=/data \
    PYTHONUNBUFFERED=1 \
    FOLIVOX_UI_HOST=0.0.0.0 \
    FOLIVOX_UI_PORT=8765 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/opt/folivox/lib/python3.10/site-packages/nvidia/cudnn/lib:/opt/folivox/lib/python3.10/site-packages/nvidia/cublas/lib

USER folivox
WORKDIR /data
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/' % os.environ.get('FOLIVOX_UI_PORT', '8765'), timeout=4)"

ENTRYPOINT ["tini", "--", "folivox-entrypoint"]
CMD ["ui", "--no-open"]
