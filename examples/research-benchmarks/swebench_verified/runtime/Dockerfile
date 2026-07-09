ARG BASE_IMAGE=python:3.12-slim
ARG OPENEVO_SWEBENCH_IMAGE_VERSION=1
FROM ${BASE_IMAGE}

ARG OPENEVO_SWEBENCH_IMAGE_VERSION=1
LABEL io.openevo.swebench-image-version=${OPENEVO_SWEBENCH_IMAGE_VERSION}

ENV DEBIAN_FRONTEND=noninteractive \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/openevo \
    NPM_CONFIG_PREFIX=/home/openevo/.npm-global \
    PATH=/home/openevo/.npm-global/bin:/opt/miniconda3/envs/testbed/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && useradd -m -s /bin/bash openevo \
    && mkdir -p /home/openevo/.npm-global /openevo/session \
    && chown -R openevo:openevo /home/openevo /openevo/session \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

USER openevo
WORKDIR /testbed
