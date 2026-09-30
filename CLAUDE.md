# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A ZEDEDA-branded Flask demo (SMAGIC'26) that runs YOLO11 PPE detection on switchable video sources and serves an annotated MJPEG feed, live JSON stats and a source-control API. Target: OnLogic CL260 (Intel N150, amd64) running EVE-OS 17, managed from the hummingbird ZEDEDA Cloud cluster. There is no test suite or linter; verify by running the container (see below). Demo runbook: `docs/SMAGIC26-demo.md`.

## Commands

```bash
# Native build + run on the Mac, with a fake "cloud stream" served from sample-videos/
docker build --platform linux/arm64 -t ppe-stream:dev-arm64 .
(cd sample-videos && python3 -m http.server 8089) &
docker run --rm -p 8001:8000 -e CAMERA_STREAM_URL=http://host.docker.internal:8089/playlist.m3u8 ppe-stream:dev-arm64
# Kill/restart the http.server to exercise auto failover; watch GET /stats (source.active, uplink.state)

# Device image (amd64), pushed to Docker Hub for EVE
docker buildx build --platform linux/amd64 -t sergeyzededa/zdemo:6 --push .

# ZEDEDA Cloud objects (token in ZEDEDA_TOKEN, never in the repo)
./deploy/zededa_deploy.py image|app|deploy|status|undeploy
```

The EVE node is reachable at `ssh root@192.168.0.108` on the home LAN (debug SSH key configured); `lsusb -t` there maps USB ports to `usbaddr`.

## Architecture

- `web_yolo.py`: one `Pipeline` thread owns the active source, runs inference and publishes the latest JPEG through a `Condition`; every `/video_feed` client just reads that frame, so viewers don't multiply CPU. Inference runs whether or not anyone is watching.
- Sources: `VideoFileSource` (URL or list of files, loops, real-time pacing by skipping frames with `grab()`) and `LiveSource` (camera read on its own thread, newest frame wins) wrapping either `V4L2Backend` or `uvc_capture.UvcCamera`.
- Auto mode (`Pipeline._wanted`): `stream` while `UplinkMonitor` says the stream URL is reachable, else `usb` if `UsbMonitor` sees a camera, else `local`. A failing source gets a 10 s cooldown; a stream read failure marks the uplink down immediately. Only `http(s)` URLs are probed; other schemes are treated as online until a read fails.
- `/stats` keeps the original keys (`detections_per_class`, `confidence_scores`, `fps`, `last_detection_time`, `detection_history`) and adds `inference_ms`, `classes`, `source`, `uplink`, `device`. The UI (`templates/index.html`, fully self-contained, polls every 1 s) depends on these.
- Class colours are defined twice and must stay in sync: `CLASS_COLORS` (BGR) in `web_yolo.py` and `COLORS` in the template JS.
- `yolo.py` is a separate desktop viewer (`cv2.imshow`) and is not part of the image.

## EVE-OS constraints (why things are the way they are)

- EVE runs containers inside a KVM VM booted with the EVE kernel, which has **no uvcvideo** driver, so a passed-through USB camera never becomes `/dev/video*`. `uvc_capture.py` uses libuvc over libusb (`/dev/bus/usb`, devtmpfs is mounted in the container) and decodes MJPEG with OpenCV. Keep the ctypes struct/enum assumptions in that file aligned with Debian bookworm's libuvc 0.0.6.
- The page must work offline (the demo cuts the uplink): no external fonts, scripts or images. The logo is `static/zededa-logo.svg`.
- USB camera: TANDBERG PrecisionHD (`1f82:0001`) on usbaddr `3:4`. MJPEG up to 1280x720@30. Assign the whole controller `USB1` (PCI 00:14.0, `IO_TYPE_USB_CONTROLLER`): per-port passthrough goes through QEMU's emulated xHCI and the isochronous stream never delivers frames.
- CPU speed on the CL260 is capped by firmware, not the app: turbo was off in BIOS, and even with turbo on, PL1 is 6 W (MSR 0x610; 0x64F reports "PL1 power limiting"), so sustained 4-core inference runs at ~800 MHz and ~850-1000 ms/frame. Read MSRs in the EVE debug shell with `dd if=/dev/cpu/0/msr bs=8 count=1 skip=$((0x64f)) iflag=skip_bytes | hexdump -e '1/8 "%016x\n"'`.
- ZEDEDA API quirks handled in `deploy/zededa_deploy.py`: a new container image stays CREATED until `PUT /apps/images/id/{id}/uplink`; app bundle PUT needs the full record (revision); instance ACL port maps use `mapparams: {port}` while bundles use `portmapto: {appPort}`. Base OS changes: `PUT /devices/id/{id}/publish` then `/apply` with `{"baseImage":[{"imageName":...}]}`.
- EVE app env vars come from the app's cloud-init template (`runcmd: - KEY=###KEY###`), see `VARIABLES` in `deploy/zededa_deploy.py`.

## Build notes

- Base image is pinned to `python:3.11-slim-bookworm` (for the `libuvc0` package). CPU-only torch is installed from the PyTorch index; ultralytics' GUI `opencv-python` is swapped for `opencv-python-headless` in both stages (the GUI build needs X11 libs).
- The `export` stage runs on `$BUILDPLATFORM` and produces `bestn_openvino_model/`; the runtime prefers it over `bestn.pt` unless `MODEL_PATH` is set.
- `.dockerignore` keeps the other `.pt` files, `.ts` segments, the zip and `deploy/` out of the image; `sample-videos/*.mp4` are copied to `/app/videos`.
