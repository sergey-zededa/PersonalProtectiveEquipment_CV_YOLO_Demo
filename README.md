# PPE Object Detection Web App

A Flask web app that runs a YOLO11 PPE model (gloves, hardhat, safety glasses, vest) on a video
source and serves:
- A ZEDEDA-branded UI at `/` with the annotated stream, live counts and a source switcher
- An MJPEG stream at `/video_feed`
- JSON stats at `/stats`
- Source control at `POST /api/source` with `{"mode": "auto|stream|usb|local"}`

Built for the SMAGIC'26 demo on an OnLogic CL260 running EVE-OS: the same app shows a cloud-hosted
pre-recorded stream while the node is online, and keeps detecting from a locally attached USB
camera (or on-node video files) when the uplink is cut. See `docs/SMAGIC26-demo.md` for the runbook.


## Video sources

| Mode     | What it plays                                                        |
|----------|----------------------------------------------------------------------|
| `stream` | `CAMERA_STREAM_URL` (HLS `.m3u8`, MP4, RTSP; anything OpenCV+FFmpeg opens) |
| `usb`    | USB camera: `/dev/video*` if present, otherwise userspace UVC via libuvc |
| `local`  | `*.mp4` files in `LOCAL_VIDEO_DIR` (the image bundles `sample-videos/*.mp4`) |
| `auto`   | `stream` while the uplink is reachable, else `usb` if a camera is attached, else `local` |

Auto mode probes the stream URL every few seconds (two misses to go offline, two hits to come
back), so cutting and restoring the uplink switches sources on its own. Switch manually from the UI
or with `curl -X POST -H 'Content-Type: application/json' -d '{"mode":"usb"}' http://<host>:8000/api/source`.

**Why libuvc:** EVE-OS runs container apps inside a small VM that boots the EVE kernel, which has
no `uvcvideo` driver, so a passed-through USB camera never appears as `/dev/video0`. The app reads
the camera directly over libusb instead (`uvc_capture.py`), which needs no kernel driver.

**Pass the USB controller, not the port.** Per-port assignment (`IO_TYPE_USB_DEVICE`) goes through
QEMU's emulated xHCI, which breaks the camera's isochronous transfers (frames never arrive). Assign
the whole controller (`IO_TYPE_USB_CONTROLLER`, `USB1` on the CL260) so the VM drives the real
hardware.


## Quick start (Docker)

```bash
docker build -t ppe-stream .
docker run --rm -p 8000:8000 \
  -e CAMERA_STREAM_URL="http://your-server/playlist.m3u8" \
  ppe-stream
```

Open http://localhost:8000

For the CL260 (amd64) from an Apple Silicon Mac:

```bash
docker buildx build --platform linux/amd64 -t sergeyzededa/zdemo:6 --push .
```

The build exports the model to OpenVINO IR in a separate stage (runs natively on the build
machine; the IR is architecture-independent). OpenVINO is roughly 2 to 3 times faster than PyTorch
on Intel CPUs.


### Environment variables

| Variable            | Default                          | Notes |
|---------------------|----------------------------------|-------|
| `CAMERA_STREAM_URL` | `https://sspm.freeddns.org/videos/playlist.m3u8` | `off` disables the cloud stream |
| `SOURCE_MODE`       | `auto`                           | Start-up mode |
| `USB_CAMERA`        | `auto`                           | `auto`, `uvc`, `off`, `/dev/videoN` or an index |
| `USB_RESOLUTION`    | `1280x720`                       | MJPEG mode requested from the camera (falls back to 1024x576, 640x480, 640x360) |
| `USB_FPS`           | `30`                             | |
| `LOCAL_VIDEO_DIR`   | `videos`                         | |
| `MODEL_PATH`        | `bestn_openvino_model` if present, else `bestn.pt` | Any Ultralytics-loadable model |
| `IMGSZ` / `CONF`    | `640` / `0.3`                    | Inference size and confidence threshold |
| `REALTIME_PLAYBACK` | `1`                              | Skip frames so recorded clips play at natural speed |
| `DEVICE_LABEL`      | hostname                         | Shown under the page title |
| `PORT`              | `8000`                           | |


## Local development (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
CAMERA_STREAM_URL=off LOCAL_VIDEO_DIR=sample-videos MODEL_PATH=bestn.pt python web_yolo.py
```

To fake the cloud stream and test failover, serve the bundled HLS playlist and stop/start it:

```bash
(cd sample-videos && python3 -m http.server 8089)
CAMERA_STREAM_URL=http://localhost:8089/playlist.m3u8 LOCAL_VIDEO_DIR=sample-videos python web_yolo.py
```


## Deploying on ZEDEDA Cloud

`deploy/zededa_deploy.py` registers (and uplinks) the image, creates or updates the edge app and
deploys an instance with the USB controller attached (defaults: hummingbird cluster,
`sergey-cl260`, controller `USB1`). The token is read from `ZEDEDA_TOKEN`.

```bash
export ZEDEDA_TOKEN=...
./deploy/zededa_deploy.py image && ./deploy/zededa_deploy.py app
./deploy/zededa_deploy.py deploy
./deploy/zededa_deploy.py status
```


## Troubleshooting
- USB camera shows "Not found": check the port is assigned to the app instance (the `status`
  action prints the assignment), and that the camera enumerates on the node (`lsusb` in the EVE
  debug shell).
- Cloud stream shows "Offline" while the node is online: the node cannot reach `CAMERA_STREAM_URL`;
  test it with `curl` from the same network.
- Low FPS: check the CPU is not stuck at base clock (`cat /sys/devices/system/cpu/intel_pstate/no_turbo`
  in the EVE debug shell; the CL260 ships with turbo off in the BIOS), that the OpenVINO model is
  loaded (footer shows the model name), or lower `IMGSZ`.


## License
This project contains third-party components (Ultralytics YOLO, OpenCV, OpenVINO, libuvc) under
their respective licenses. Review their terms before production use.
