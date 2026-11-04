# syntax=docker/dockerfile:1
#
# DAVbridge: calendar bridge between Outlook (EWS, Exchange ActiveSync 14.1) and a CalDAV server.
#
# Multi-stage build:
#   builder  installs the hash-pinned dependencies (requirements/runtime.txt) into a virtual environment and builds the project wheel
#            with the hash-pinned build tools (requirements/build.txt, `--no-build-isolation`: nothing is fetched without a hash),
#   runtime  copies only the runtime environment plus the licence files onto the same base image.
# No compiler, pip, git or curl is part of the final image, and there are no architecture-specific
# binaries of our own: the same Dockerfile builds linux/amd64 and linux/arm64 (all dependencies ship manylinux wheels).
#
# Both stages use the same multi-architecture manifest list of python:3.12-slim-trixie, pinned by digest (a test keeps the two
# lines identical). Dependabot proposes updates to both.
FROM python:3.12-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
# Two environments: /opt/venv (runtime, copied to the final image, without pip) and /opt/buildenv (build tools, discarded with this stage).
# The pip that ships with the base image is used as it is; it is never upgraded.
RUN python -m venv --without-pip /opt/venv && python -m venv /opt/buildenv
COPY requirements/runtime.txt requirements/build.txt ./requirements/
RUN /opt/buildenv/bin/pip install --require-hashes --only-binary=:all: -r requirements/build.txt \
    && /opt/buildenv/bin/pip --python /opt/venv/bin/python install --require-hashes --only-binary=:all: -r requirements/runtime.txt
COPY pyproject.toml LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src ./src
COPY data ./data
RUN /opt/buildenv/bin/pip wheel --no-index --no-build-isolation --no-deps --wheel-dir /wheels . \
    && /opt/buildenv/bin/pip --python /opt/venv/bin/python install --no-index --no-deps --no-compile /wheels/*.whl


FROM python:3.12-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS runtime
ARG VERSION=0.1.0
ARG REVISION=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="DAVbridge" \
      org.opencontainers.image.description="Independent calendar bridge: Outlook (EWS and Exchange ActiveSync 14.1) to CalDAV" \
      org.opencontainers.image.source="https://github.com/chr0mcom/davbridge" \
      org.opencontainers.image.url="https://github.com/chr0mcom/davbridge" \
      org.opencontainers.image.documentation="https://github.com/chr0mcom/davbridge/tree/main/docs" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${BUILD_DATE}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    GIT_COMMIT=${REVISION} \
    DAVBRIDGE_CONFIG=/etc/davbridge/davbridge.toml DAVBRIDGE_STATE_DIR=/var/lib/davbridge
COPY --from=builder /opt/venv /opt/venv
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /usr/share/doc/davbridge/
# Remove the package installer from the final image (the application is installed already) and prepare the state directory:
# it exists with the final owner so that a freshly created named volume is initialised correctly. All state lives in this
# one volume; the root file system can stay read-only.
RUN /usr/local/bin/python -m pip uninstall --yes pip \
    && install -d -m 0755 /etc/davbridge \
    && install -d -o 65532 -g 65532 -m 0700 /var/lib/davbridge
VOLUME ["/var/lib/davbridge"]
# Fixed numeric user without a passwd entry; there is no way back to root.
USER 65532:65532
WORKDIR /var/lib/davbridge
EXPOSE 8000
# `davbridge healthcheck` reads server.listen from the configuration and probes /healthz on the loopback interface.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 CMD ["davbridge", "healthcheck"]
# Exactly one uvicorn worker on server.listen (default 0.0.0.0:8000). Forwarded headers are evaluated by the application
# itself, and only from server.trusted_proxies, so uvicorn's own proxy-header handling is off (see `davbridge serve`).
CMD ["davbridge", "serve"]
