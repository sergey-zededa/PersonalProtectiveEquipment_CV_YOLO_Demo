# syntax=docker/dockerfile:1
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps for OpenCV + video
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY templates ./templates
COPY web_yolo.py ./
# Copy default model (override via MODEL_PATH if needed)
COPY bestn.pt ./

# Runtime envs (override at `docker run`)
ENV CAMERA_STREAM_URL="" \
    MODEL_PATH="bestn.pt" \
    PORT=5000

EXPOSE 5000

CMD ["python", "web_yolo.py"]
