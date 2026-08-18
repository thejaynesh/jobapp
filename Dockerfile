FROM python:3.12-slim

# Install system deps: pdflatex, playwright deps, and the postgres client.
#
# `postgresql-client` is unversioned on purpose. It provides pg_dump and psql
# for the nightly backup and for restoring from one, and the only rule that
# matters is directional: pg_dump can dump a server OLDER than itself but
# refuses one that is newer. Debian's default client tracks the base image and
# has always been at least as new as the server this deploys against, so
# naming a version here would only create a way for the two to drift — an
# earlier revision of this file pinned 16 from the PGDG repo under a hardcoded
# `bookworm`, which broke the moment python:3.12-slim moved to trixie.
RUN apt-get update && apt-get install -y \
    texlive-latex-base \
    texlive-fonts-recommended \
    texlive-latex-extra \
    curl \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Fail the build here rather than at 3am on the first backup. A missing pg_dump
# is a broken image, and finding that out from a backup job that has silently
# never run is the failure this whole feature exists to prevent.
RUN pg_dump --version && psql --version

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev]"

# Install playwright chromium for scraping
RUN playwright install-deps chromium || true
RUN playwright install chromium

COPY . .

RUN mkdir -p /storage/resumes /storage/cover_letters /storage/tex
