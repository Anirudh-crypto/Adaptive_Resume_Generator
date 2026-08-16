FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    libgraphite2-3 libharfbuzz0b libfontconfig1 libicu-dev \
    && rm -rf /var/lib/apt/lists/*

# Tectonic is a single self-contained binary -- no TeX Live install needed.
RUN curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh \
    && mv tectonic /usr/local/bin/tectonic \
    && chmod +x /usr/local/bin/tectonic

WORKDIR /app

# Pre-warm Tectonic's package bundle cache so runtime compiles need no network access and cold
# starts aren't paying for a package download.
#
# Ordering matters here, and it is the whole reason prewarm.tex exists as a static file. This
# layer is a ~100MB bundle download. If it sat *after* `COPY app/`, every single code change
# would invalidate it -- re-downloading the bundle on every build and pushing a fresh ~100MB
# layer to Artifact Registry each time, which blows through the free storage tier within a
# handful of deploys. Keeping it above the code copy means it is built once and then reused.
COPY latex_templates/ latex_templates/
RUN tectonic --untrusted -o /tmp latex_templates/prewarm.tex \
    && tectonic --untrusted -o /tmp latex_templates/prewarm_in.tex \
    && rm -f /tmp/prewarm.pdf /tmp/prewarm_in.pdf

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

ENV TECTONIC_BIN=tectonic
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
