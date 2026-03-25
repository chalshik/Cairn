# ---------------------------------------------------------------------------
# Stage 1 — dependency builder
# Installs Python packages in an isolated layer so source-code changes
# don't trigger a full pip + Playwright re-install.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# System libraries required by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates \
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        fonts-liberation libappindicator3-1 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Install Playwright browsers into a fixed path so they can be cached
ENV PLAYWRIGHT_BROWSERS_PATH=/pw-browsers
RUN pip install --no-cache-dir playwright \
    && playwright install chromium --with-deps \
    && rm -rf /var/lib/apt/lists/*


# ---------------------------------------------------------------------------
# Stage 2 — runtime image
# Copies only what's needed; keeps the final image as small as possible.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# dumb-init: proper PID-1 signal forwarding so SIGTERM/SIGINT reach Python
RUN apt-get update && apt-get install -y --no-install-recommends \
        dumb-init \
        # Chromium runtime libraries
        libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local
COPY --from=builder /pw-browsers /pw-browsers

# Non-root user for security
RUN useradd -m -u 1000 cairn
USER cairn
WORKDIR /app

# Application source
COPY --chown=cairn:cairn . .

# Runtime environment
ENV PLAYWRIGHT_BROWSERS_PATH=/pw-browsers
ENV CAIRN_TRANSPORT=sse
ENV CAIRN_PORT=8000
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Health check: verify the SSE port is accepting connections
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c \
        "import socket; s=socket.socket(); s.settimeout(5); \
         s.connect(('127.0.0.1', 8000)); s.close()" \
    || exit 1

ENTRYPOINT ["dumb-init", "--"]
CMD ["python3", "main.py"]
