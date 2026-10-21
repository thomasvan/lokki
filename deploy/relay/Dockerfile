# The relay (core/) as an image. How to build, run, upgrade and roll back: docs/relay/docker.md.
# Build it with deploy/relay/build.sh, which builds a COMMIT (git archive), so the image holds exactly the tracked
# files of that commit and nothing from the working tree (config.yaml, auths/, logs/ are untracked and never in it).
#
# The runtime image has no `.git` and sets no `RELAY_REPO_ROOT`, so the relay's in-repository guard is off here; the
# code sits at /app/core for readability only. The test stage sets `RELAY_REPO_ROOT=/src` so the suite's guard tests
# run against the source tree.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:f85c5697265c178cc6887276c55fe16cf3d14ca35c3df6a5eab3b360534a55d2

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY deploy/relay/requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

# `test`: the whole offline suite on the interpreter the image ships. Not part of the runtime image.
FROM base AS test
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
ENV RELAY_REPO_ROOT=/src
COPY . /src
WORKDIR /src/core
RUN python3 -m unittest discover -s . -p "test_*.py"

FROM base AS runtime
ARG REVISION
RUN case "${REVISION}" in (""|*[!0-9a-f]*) bad=1 ;; esac; \
    if [ -n "${bad:-}" ] || [ "${#REVISION}" -lt 7 ] || [ "${#REVISION}" -gt 64 ]; then \
      echo "build with --build-arg REVISION=<commit id: 7 to 64 lowercase hex digits> (deploy/relay/build.sh)"; exit 1; fi
WORKDIR /app/core
COPY core/server.py ./server.py
COPY core/lokki_core ./lokki_core
COPY core/VERSION ./VERSION
COPY deploy/relay/healthcheck.py ./healthcheck.py
# The revision, written down twice: the file is what the relay reads in an image (it has no checkout and no git);
# the variable shows in `docker inspect` and wins if both are present, so they are made identical here.
RUN printf '%s\n' "${REVISION}" > REVISION
ENV RELAY_REVISION=${REVISION} \
    RELAY_CONFIG=/etc/relay/config.yaml
LABEL org.opencontainers.image.revision=${REVISION} \
      org.opencontainers.image.title="clisaving relay" \
      org.opencontainers.image.source="core/"
USER 1000:1000
ENTRYPOINT ["python3", "/app/core/server.py"]
CMD ["--config", "/etc/relay/config.yaml"]
