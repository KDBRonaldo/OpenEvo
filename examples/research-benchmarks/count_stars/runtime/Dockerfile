FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ARG OPENEVO_COUNT_STARS_IMAGE_VERSION=1

COPY --from=node /usr/local/ /usr/local/

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/openevo \
    NPM_CONFIG_PREFIX=/home/openevo/.local \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PATH=/home/openevo/.local/bin:/home/openevo/.venv/bin:${PATH}

LABEL io.openevo.count-stars-image-version="${OPENEVO_COUNT_STARS_IMAGE_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        python-is-python3 \
        sudo \
        tmux \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash openevo \
    && echo 'openevo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers \
    && mkdir -p /openevo/session /home/openevo/.local /home/openevo/.venv \
    && chown -R openevo:openevo /openevo/session /home/openevo

RUN echo 'export PATH=/home/openevo/.local/bin:/home/openevo/.venv/bin:$PATH' \
    > /etc/profile.d/openevo-path.sh

USER openevo

WORKDIR /openevo/session

