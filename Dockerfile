FROM python:3.11-slim

# Install system deps needed by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    fonts-liberation libappindicator3-1 xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright and its Chromium browser
RUN playwright install chromium --with-deps

COPY . .

# Default to SSE transport so the container runs as a real HTTP server
ENV CAIRN_TRANSPORT=sse
ENV CAIRN_PORT=8000

EXPOSE 8000

CMD ["python", "main.py"]
