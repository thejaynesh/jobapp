FROM python:3.12-slim

# Install system deps: pdflatex + playwright deps
RUN apt-get update && apt-get install -y \
    texlive-latex-base \
    texlive-fonts-recommended \
    texlive-latex-extra \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# pg_dump and psql, for the nightly backup and for restoring from one.
#
# From PGDG rather than Debian, and pinned to 16 to match the server image.
# pg_dump refuses to dump a server newer than itself, and bookworm ships 15 —
# so the distro package would produce a backup task that fails every night with
# a version-mismatch error, which is a worse outcome than no backup task at all
# because it looks like one.
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev]"

# Install playwright chromium for scraping
RUN playwright install-deps chromium || true
RUN playwright install chromium

COPY . .

RUN mkdir -p /storage/resumes /storage/cover_letters /storage/tex
