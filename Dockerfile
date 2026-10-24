FROM python:3.11-slim

# Install ffmpeg, curl, ca-certificates, and nodejs (required by yt-dlp for YouTube BotGuard JS challenge solving)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure yt-dlp is updated to the newest version
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
