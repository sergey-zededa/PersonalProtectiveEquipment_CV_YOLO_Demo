# PPE Object Detection Web App

A lightweight Flask web app that runs Ultralytics YOLO on a live video source and serves:
- A branded UI at `/` that shows the annotated MJPEG stream and live stats
- An MJPEG stream at `/video_feed`
- JSON stats at `/stats`

The UI styling matches ZEDEDA’s two‑tone corner motif.


## Requirements
- A camera/video stream URL the app can read (HLS `.m3u8`, MP4 `.mp4`, or other sources OpenCV + FFmpeg can decode)
- A YOLO model file (default is the included `bestn.pt`). You can override this via `MODEL_PATH`.


## Quick start (Docker)

Build the image:

```bash path=null start=null
docker build -t ppe-stream .
```

Run with your camera URL (port 5000 by default):

```bash path=null start=null
docker run --rm \
  -p 5000:5000 \
  -e CAMERA_STREAM_URL="http://your-device-or-server/playlist.m3u8" \
  ppe-stream
```

Open http://localhost:5000


### Environment variables
- `CAMERA_STREAM_URL` (string): URL to your video stream (e.g., HLS `.m3u8`, MP4). If empty, the app uses its internal default.
- `MODEL_PATH` (string, default: `bestn.pt`): Path to a YOLO model inside the container. You can mount your own.
- `PORT` (int, default: `5000`): HTTP port the Flask app binds to inside the container.

Examples:

Use a custom model that you mount into the container:

```bash path=null start=null
docker run --rm \
  -p 5000:5000 \
  -e CAMERA_STREAM_URL="http://your-device/stream.m3u8" \
  -e MODEL_PATH="/app/custom.pt" \
  -v "$PWD/custom.pt:/app/custom.pt:ro" \
  ppe-stream
```

Change the listen port:

```bash path=null start=null
docker run --rm \
  -e PORT=8080 \
  -p 8080:8080 \
  -e CAMERA_STREAM_URL="http://your-device/stream.m3u8" \
  ppe-stream
```


## Local development (without Docker)

Create a virtual environment and install dependencies:

```bash path=null start=null
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the app with your stream URL:

```bash path=null start=null
export CAMERA_STREAM_URL="http://your-device/playlist.m3u8"
export MODEL_PATH="bestn.pt"   # or path to another model
export PORT=5000
python web_yolo.py
```

Open http://localhost:5000


## Endpoints
- `/` – Main page with video and stats
- `/video_feed` – MJPEG stream (multipart/x-mixed-replace)
- `/stats` – JSON payload of live stats (per-class counts, confidences, fps, timestamps)


## Docker Compose (optional)

```yaml path=null start=null
services:
  ppe:
    build: .
    image: ppe-stream
    ports:
      - "5000:5000"
    environment:
      CAMERA_STREAM_URL: "http://your-device/playlist.m3u8"
      MODEL_PATH: "bestn.pt"
      PORT: 5000
    # Mount a custom model if desired
    # volumes:
    #   - ./custom.pt:/app/custom.pt:ro
```

Start it with:

```bash path=null start=null
docker compose up --build
```


## Troubleshooting
- Stream doesn’t play:
  - Verify the URL is reachable from inside the container: `docker exec -it <cid> ffprobe <url>`
  - If it’s RTSP or another protocol, ensure the URL format and network reachability are correct.
- Model not found:
  - Check `MODEL_PATH` and any volume mount path you used.
- High CPU:
  - Running YOLO inference on CPU can be heavy. Consider smaller models or hardware acceleration.


## Notes
- Default model: `bestn.pt` (present in the repo/image by default). Override with `MODEL_PATH` as needed.
- Accepted sources: anything OpenCV compiled with FFmpeg can read (HLS `.m3u8`, MP4, etc.).


## License
This project contains third-party components (Ultralytics YOLO, OpenCV) under their respective licenses. Review their terms before production use.
