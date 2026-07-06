FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY transcribe.py .

# Whisper models are cached here; mount a volume to persist across restarts.
ENV HF_HOME=/models
VOLUME /models

CMD ["python", "transcribe.py"]
