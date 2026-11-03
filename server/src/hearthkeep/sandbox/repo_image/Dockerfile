# Hearthkeep's repo sandbox: repo dependencies are installed and repo tests run in it.
# Built by `hearthkeep sandbox build --data <data>`; the tag is a hash of this folder, so any
# change here names a new image. Every container gets its command as `sh -c`.
FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS node
FROM ghcr.io/astral-sh/uv:0.13.2@sha256:b9fddcde095a7aeafd499024adb8927df6619c7c5672f5ff7126474715f62ba4 AS uv

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /usr/local/bin/uv
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version && npm --version && uv --version

RUN useradd --uid 10001 --create-home hk \
    && mkdir -p /deps /repo/node_modules /src/node_modules \
    && chown 10001:10001 /deps /repo /repo/node_modules /src /src/node_modules

# A new dependency volume takes its owner from the folder it is first mounted on (above).
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 LANG=C.UTF-8 NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false \
    UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /repo
USER 10001:10001
