FROM python:3.11-slim

# Install FFmpeg, Node.js (JS runtime for yt-dlp), and certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Create writable data dir (for non-root cloud runners like Hugging Face Spaces)
RUN chmod -R 777 /app

ENV PORT=7860
EXPOSE 7860

CMD ["python", "bot.py"]
