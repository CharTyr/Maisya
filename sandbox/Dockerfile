ARG NODE_IMAGE=node:24-bookworm-slim
ARG PYTHON_IMAGE=python:3.13-slim-bookworm

FROM ${NODE_IMAGE} AS node_runtime
FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node_runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && groupadd --gid 10000 sandbox \
    && useradd --uid 10000 --gid 10000 --create-home --home-dir /home/sandbox --shell /bin/sh sandbox \
    && mkdir -p /work \
    && chown 10000:10000 /work

ENV LANG=C.UTF-8 \
    HOME=/home/sandbox \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /work
USER 10000:10000
