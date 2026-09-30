# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

# ---- Stage 1: export the model to OpenVINO IR --------------------------------
# Runs on the build machine's native arch; the IR (.xml/.bin) is portable.
FROM --platform=$BUILDPLATFORM ${PYTHON_IMAGE} AS export
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir "ultralytics>=8.3.0" "openvino>=2024.4" \
 && pip uninstall -y opencv-python \
 && pip install --no-cache-dir --no-deps --force-reinstall "opencv-python-headless>=4.8.0"
WORKDIR /export
COPY bestn.pt ./
RUN yolo export model=bestn.pt format=openvino imgsz=640 \
 && test -f bestn_openvino_model/bestn.xml

# ---- Stage 2: runtime --------------------------------------------------------
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    YOLO_OFFLINE=1

# libuvc/libusb: userspace USB camera access (EVE's kernel has no uvcvideo driver)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libuvc0 \
    libusb-1.0-0 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch keeps the image small; swap ultralytics' opencv for the headless build (no libGL)
COPY requirements.txt ./
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y opencv-python \
 && pip install --no-cache-dir --no-deps --force-reinstall "opencv-python-headless>=4.8.0"

COPY bestn.pt ./
COPY --from=export /export/bestn_openvino_model ./bestn_openvino_model
COPY sample-videos/*.mp4 ./videos/
COPY static ./static
COPY templates ./templates
COPY web_yolo.py uvc_capture.py ./

# Runtime envs (override at `docker run` or through EVE cloud-init variables)
ENV PORT=8000

EXPOSE 8000

CMD ["python", "web_yolo.py"]
