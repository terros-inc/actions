ARG NODE_VERSION=24
FROM node:${NODE_VERSION}-bookworm-slim

ARG PI_VERSION=1.1.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --ignore-scripts --no-audit --no-fund "@earendil-works/pi-coding-agent@${PI_VERSION}"

WORKDIR /workspace
