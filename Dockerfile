FROM python:3.12-slim

# Install system deps: pdflatex + playwright deps
RUN apt-get update && apt-get install -y \
    texlive-latex-base \
    texlive-fonts-recommended \
    texlive-latex-extra \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[dev]"

# Install playwright chromium for scraping
RUN playwright install-deps chromium || true
RUN playwright install chromium

COPY . .

RUN mkdir -p /storage/resumes /storage/cover_letters /storage/tex
