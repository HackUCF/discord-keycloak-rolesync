# Stage 1: Set up the build envrionment
# Do this in a separate stage to prevent code updates from retriggering a build env update
# Also makes it look neater :)
FROM debian:12-slim AS builder

RUN apt-get update && apt-get install --no-install-suggests --no-install-recommends --yes pipenv
ADD https://github.com/pyenv/pyenv.git#v2.6.11 /pyenv/


# Stage 2: Configure the Python venv
FROM builder AS builder-pipenv

ENV PIPENV_VENV_IN_PROJECT=1
ENV PYENV_ROOT=/pyenv/

COPY Pipfile /app/
WORKDIR /app

RUN pipenv install --skip-lock


# Stage 3: Run in a distroless image
# Reduces final image size & removes anything not required for running the app itself
# Less size, less points of traversal, less problems
FROM gcr.io/distroless/python3-debian12

LABEL org.opencontainers.image.title="Discord to Keycloak Role Sync"
LABEL org.opencontainers.image.description="Synchronises membership of Discord roles to Keycloak groups"
LABEL org.opencontainers.image.authors="Ike Johnson-Woods <contact@ike.au>"

LABEL org.opencontainers.image.source=https://github.com/HackUCF/discord-keycloak-rolesync
LABEL org.opencontainers.image.license=MPL-2.0

COPY --from=builder-pipenv /app/.venv/ /venv/
COPY app.py /app/app.py
WORKDIR /app

ENTRYPOINT ["/venv/bin/python", "app.py"]
