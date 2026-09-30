import glob
import os
import socket
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from ultralytics import YOLO

import uvc_capture

# ---------------------------------------------------------------------------
# Configuration (all overridable through env / EVE cloud-init variables)
# ---------------------------------------------------------------------------
def _default_model():
    return 'bestn_openvino_model' if os.path.isdir('bestn_openvino_model') else 'bestn.pt'

MODEL_PATH = os.environ.get('MODEL_PATH') or _default_model()
VIDEO_URL = os.environ.get('CAMERA_STREAM_URL') or 'https://sspm.freeddns.org/videos/playlist.m3u8'
if VIDEO_URL.lower() == 'off':
    VIDEO_URL = ''
LOCAL_VIDEO_DIR = os.environ.get('LOCAL_VIDEO_DIR', 'videos')
USB_CAMERA = os.environ.get('USB_CAMERA', 'auto')        # auto | uvc | off | /dev/videoN | <index>
USB_W, USB_H = (int(v) for v in os.environ.get('USB_RESOLUTION', '1280x720').lower().split('x'))
USB_FPS = int(os.environ.get('USB_FPS', '30'))
SOURCE_MODE = os.environ.get('SOURCE_MODE', 'auto')
IMGSZ = int(os.environ.get('IMGSZ', '640'))
CONF = float(os.environ.get('CONF', '0.3'))
REALTIME_PLAYBACK = os.environ.get('REALTIME_PLAYBACK', '1') != '0'
UPLINK_PROBE_SEC = float(os.environ.get('UPLINK_PROBE_SEC', '3'))
DEVICE_LABEL = os.environ.get('DEVICE_LABEL') or socket.gethostname()

SOURCES = {
    'stream': {'label': 'Cloud stream', 'detail': 'Pre-recorded CCTV, streamed over the uplink'},
    'usb':    {'label': 'USB camera',   'detail': 'Live feed from the camera on this node'},
    'local':  {'label': 'On-node video', 'detail': 'Pre-recorded CCTV, stored on the node'},
}
MODES = ['auto'] + list(SOURCES)

# ZEDEDA palette in BGR, one colour per class (hardhat stays signature orange)
CLASS_COLORS = {'hardhat': (0, 80, 255), 'vest': (213, 219, 110), 'gloves': (71, 189, 245),
                'safety glasses': (77, 194, 124)}
EXTRA_COLORS = [(155, 74, 198), (104, 88, 36)]
PETROL = (68, 56, 19)
WHITE = (255, 255, 255)


class SourceError(Exception):
    pass


# ---------------------------------------------------------------------------
# Video sources
# ---------------------------------------------------------------------------
class VideoFileSource:
    """HLS/MP4/RTSP URL or a folder of files; loops forever, optionally paced to real time."""
    live = False

    def __init__(self, targets, timeout_ms=6000):
        self.targets = targets
        self.timeout_ms = timeout_ms
        self.idx = -1
        self.cap = None
        self._open_next()

    def _open_next(self):
        if self.cap:
            self.cap.release()
        self.idx = (self.idx + 1) % len(self.targets)
        target = self.targets[self.idx]
        self.cap = cv2.VideoCapture(target, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.timeout_ms])
        if not self.cap.isOpened():
            raise SourceError(f'cannot open {target}')
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        self.fps = fps if 1 < fps < 121 else 25.0
        self.t0, self.consumed = time.time(), 0

    def read(self):
        if REALTIME_PLAYBACK and self.consumed:
            # Skip frames we had no time to analyse so the clip plays at its natural speed
            behind = int((time.time() - self.t0) * self.fps) - self.consumed
            for _ in range(min(behind, 60)):
                if not self.cap.grab():
                    break
                self.consumed += 1
        ok, frame = self.cap.read()
        if not ok:
            # End of clip (or a dead stream): move on / reopen; a failed open raises
            self._open_next()
            ok, frame = self.cap.read()
            if not ok:
                raise SourceError(f'no frames from {self.targets[self.idx]}')
        self.consumed += 1
        return frame

    def close(self):
        if self.cap:
            self.cap.release()


class LiveSource:
    """Camera read on its own thread so inference always gets the newest frame.

    Backends with read_raw()/decode() hand over undecoded payloads: the camera runs at 30 fps
    but only the frames inference actually picks up get decoded.
    """
    live = True

    def __init__(self, backend):
        self.backend = backend  # read() -> frame|None (or read_raw()/decode()), close()
        self._read = getattr(backend, 'read_raw', backend.read)
        self._decode = getattr(backend, 'decode', lambda x: x)
        self.cond = threading.Condition()
        self.frame, self.seq, self.err = None, 0, None
        self.running = True
        self.thread = threading.Thread(target=self._grab, daemon=True)
        self.thread.start()

    def _grab(self):
        misses = 0
        while self.running:
            try:
                frame = self._read()
            except Exception as e:  # noqa: BLE001 - surface any backend failure
                frame, self.err = None, str(e)
            if frame is None:
                misses += 1
                if misses >= 3 and not self.err:
                    self.err = 'camera stopped delivering frames'
                if self.err:
                    with self.cond:
                        self.cond.notify_all()
                    return
                continue
            misses = 0
            with self.cond:
                self.frame, self.seq = frame, self.seq + 1
                self.cond.notify_all()

    def read(self):
        for _ in range(3):  # a corrupt MJPEG frame just means waiting for the next one
            with self.cond:
                last = self.seq
                if not self.cond.wait_for(lambda: self.seq != last or self.err, timeout=5):
                    raise SourceError('camera timed out')
                if self.err:
                    raise SourceError(self.err)
                raw = self.frame
            frame = self._decode(raw)
            if frame is not None:
                return frame
        raise SourceError('camera keeps sending undecodable frames')

    def close(self):
        # A wedged USB stream can block libuvc's stop call forever; never let that stall the pipeline
        self.running = False
        closer = threading.Thread(target=self._close_backend, daemon=True)
        closer.start()
        closer.join(timeout=4)
        if closer.is_alive():
            print('Camera did not close cleanly; abandoning it')

    def _close_backend(self):
        self.thread.join(timeout=3)
        self.backend.close()


class V4L2Backend:
    def __init__(self, dev):
        self.cap = cv2.VideoCapture(dev, cv2.CAP_V4L2 if isinstance(dev, str) else cv2.CAP_ANY)
        if not self.cap.isOpened():
            raise SourceError(f'cannot open camera {dev}')
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, USB_W)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, USB_H)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def read(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        self.cap.release()


def _v4l2_devices():
    return sorted(glob.glob('/dev/video*'))


def usb_camera_present():
    if USB_CAMERA == 'off':
        return False
    if USB_CAMERA not in ('auto', 'uvc'):
        return True  # explicit device: let open() decide
    if USB_CAMERA == 'auto' and _v4l2_devices():
        return True
    return uvc_capture.camera_present()


def open_usb():
    if USB_CAMERA == 'off':
        raise SourceError('USB camera disabled (USB_CAMERA=off)')
    if USB_CAMERA not in ('auto', 'uvc'):
        dev = int(USB_CAMERA) if USB_CAMERA.isdigit() else USB_CAMERA
        return LiveSource(V4L2Backend(dev))
    if USB_CAMERA == 'auto' and _v4l2_devices():
        return LiveSource(V4L2Backend(_v4l2_devices()[0]))
    if not uvc_capture.available():
        raise SourceError('no /dev/video* and libuvc is not installed')
    try:
        cam = uvc_capture.UvcProcessCamera(USB_W, USB_H, USB_FPS)
    except RuntimeError as e:
        raise SourceError(str(e)) from e
    print(f'USB camera opened through libuvc at {cam.mode[0]}x{cam.mode[1]}@{cam.mode[2]}')
    return LiveSource(cam)


def local_videos():
    return sorted(glob.glob(os.path.join(LOCAL_VIDEO_DIR, '*.mp4')))


def open_source(key):
    if key == 'stream':
        if not VIDEO_URL:
            raise SourceError('CAMERA_STREAM_URL is not set')
        return VideoFileSource([VIDEO_URL])
    if key == 'usb':
        return open_usb()
    if key == 'local':
        files = local_videos()
        if not files:
            raise SourceError(f'no .mp4 files in {LOCAL_VIDEO_DIR}')
        return VideoFileSource(files)
    raise SourceError(f'unknown source {key}')


# ---------------------------------------------------------------------------
# Background monitors
# ---------------------------------------------------------------------------
class UplinkMonitor(threading.Thread):
    """Is the cloud stream reachable? Two misses to go offline, two hits to come back."""

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.probeable = VIDEO_URL.startswith(('http://', 'https://'))
        self.online = bool(VIDEO_URL) and not self.probeable  # optimistic for rtsp etc.
        self.state = 'unknown' if self.probeable else ('online' if VIDEO_URL else 'disabled')
        self.last_ok = None
        self._hits = self._misses = 0

    def run(self):
        while self.probeable:
            ok = self._probe()
            with self.lock:
                if ok:
                    self._hits, self._misses = self._hits + 1, 0
                    self.last_ok = time.time()
                    if not self.online and (self.state == 'unknown' or self._hits >= 2):
                        self.online, self.state = True, 'online'
                else:
                    self._hits, self._misses = 0, self._misses + 1
                    if self.state == 'unknown' or (self.online and self._misses >= 2):
                        self.online, self.state = False, 'offline'
            time.sleep(UPLINK_PROBE_SEC)

    def _probe(self):
        try:
            with urllib.request.urlopen(VIDEO_URL, timeout=3) as r:
                r.read(1)
                return 200 <= r.status < 400
        except Exception:  # noqa: BLE001 - any failure means unreachable
            return False

    def mark_down(self):
        with self.lock:
            self.online, self.state, self._hits = False, 'offline', 0

    def snapshot(self):
        with self.lock:
            return {'state': self.state, 'online': self.online, 'url': VIDEO_URL,
                    'last_ok': datetime.fromtimestamp(self.last_ok).strftime('%H:%M:%S') if self.last_ok else None}


class UsbMonitor(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.present = usb_camera_present()

    def run(self):
        while True:
            try:
                self.present = usb_camera_present()
            except Exception:  # noqa: BLE001
                self.present = False
            time.sleep(5)


# ---------------------------------------------------------------------------
# Inference pipeline: one worker, any number of viewers
# ---------------------------------------------------------------------------
def status_frame(title, subtitle=''):
    img = np.full((720, 1280, 3), PETROL, dtype=np.uint8)
    cv2.rectangle(img, (0, 0), (1280, 6), CLASS_COLORS['hardhat'], -1)
    for text, y, scale, color in ((title, 340, 1.4, WHITE), (subtitle, 400, 0.8, CLASS_COLORS['vest'])):
        (tw, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.putText(img, text, ((1280 - tw) // 2, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)
    return img


class Pipeline(threading.Thread):
    def __init__(self, model, uplink, usb):
        super().__init__(daemon=True)
        self.model, self.uplink, self.usb = model, uplink, usb
        self.mode = SOURCE_MODE if SOURCE_MODE in MODES else 'auto'
        self.active, self.source, self.last_good = None, None, None
        self.errors, self.cooldown = {}, {}
        self.switches = deque(maxlen=10)
        self.lock = threading.Lock()
        self.frame_cond = threading.Condition()
        self.jpeg, self.frame_id = None, 0
        self.fps = self.infer_ms = 0.0
        self.stats = {'detections_per_class': {}, 'confidence_scores': {}, 'last_detection_time': ''}
        self.history = deque(maxlen=20)
        self.class_names = []

    # -- source selection ---------------------------------------------------
    def set_mode(self, mode):
        with self.lock:
            self.mode = mode
            self.cooldown.clear()

    def _wanted(self):
        with self.lock:
            mode = self.mode
        if mode != 'auto':
            return mode
        now = time.time()
        if self.uplink.online and self.cooldown.get('stream', 0) < now:
            return 'stream'
        if self.usb.present and self.cooldown.get('usb', 0) < now:
            return 'usb'
        return 'local'

    def _switch(self, key):
        if self.source:
            self.source.close()
            self.source = None
        self.active = None
        self.fps = self.infer_ms = 0.0
        self._publish(status_frame(f'Connecting to {SOURCES[key]["label"]}...', SOURCES[key]['detail']))
        try:
            self.source = open_source(key)
        except SourceError as e:
            self._fail(key, str(e))
            return
        self.active = key
        self.errors.pop(key, None)
        if key != self.last_good:
            self.switches.appendleft({'time': datetime.now().strftime('%H:%M:%S'), 'from': self.last_good, 'to': key})
            self.last_good = key
        print(f'Source -> {key}')

    def _fail(self, key, msg):
        print(f'Source {key} failed: {msg}')
        self.errors[key] = msg
        if key == 'stream':
            self.uplink.mark_down()
        self.cooldown[key] = time.time() + 10
        if self.source:
            self.source.close()
            self.source = None
        self.active = None
        with self.lock:
            manual = self.mode != 'auto'
        if manual:
            self._publish(status_frame(f'{SOURCES[key]["label"]} unavailable', msg[:90]))
        time.sleep(2 if manual else 0.5)

    # -- main loop ----------------------------------------------------------
    def run(self):
        last = time.time()
        while True:
            want = self._wanted()
            if want != self.active or self.source is None:
                self._switch(want)
                if self.source is None:
                    continue
            try:
                frame = self.source.read()
            except SourceError as e:
                self._fail(self.active, str(e))
                continue
            t = time.time()
            results = self.model(frame, imgsz=IMGSZ, conf=CONF, verbose=False)
            self.infer_ms = 0.8 * self.infer_ms + 0.2 * (time.time() - t) * 1000 if self.infer_ms else (time.time() - t) * 1000
            annotated = self._annotate(frame, results[0])
            self._publish(annotated)
            now = time.time()
            dt, last = now - last, now
            if dt > 0:
                self.fps = 0.8 * self.fps + 0.2 / dt if self.fps else 1 / dt

    def _annotate(self, frame, result):
        names = result.names
        counts, confs, detections = defaultdict(int), defaultdict(list), []
        out = frame.copy()
        for box in result.boxes:
            conf = float(box.conf[0])
            label = names.get(int(box.cls[0]), str(int(box.cls[0])))
            counts[label] += 1
            confs[label].append(conf)
            detections.append({'label': label, 'confidence': conf})
            color = CLASS_COLORS.get(label.lower(), EXTRA_COLORS[int(box.cls[0]) % len(EXTRA_COLORS)])
            ink = WHITE if label.lower() == 'hardhat' else PETROL
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
            text = f'{label} {conf:.2f}'
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
            ty = max(y1, th + 6)
            cv2.rectangle(out, (x1, ty - th - 6), (x1 + tw + 8, ty), color, -1)
            cv2.putText(out, text, (x1 + 4, ty - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.6, ink, 2, cv2.LINE_AA)
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self.lock:
            self.stats = {
                'detections_per_class': dict(counts),
                'confidence_scores': {k: {'avg': sum(v) / len(v), 'min': min(v), 'max': max(v), 'all': v}
                                      for k, v in confs.items()},
                'last_detection_time': stamp,
            }
            self.history.append({'timestamp': stamp, 'detections': detections})
        return out

    def _publish(self, img):
        ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return
        with self.frame_cond:
            self.jpeg, self.frame_id = buf.tobytes(), self.frame_id + 1
            self.frame_cond.notify_all()

    # -- consumers ----------------------------------------------------------
    def frames(self):
        seen = -1
        while True:
            with self.frame_cond:
                self.frame_cond.wait_for(lambda: self.frame_id != seen, timeout=1.0)
                seen, jpeg = self.frame_id, self.jpeg
            if jpeg:
                yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n'

    def snapshot(self):
        with self.lock:
            stats = dict(self.stats)
            mode, history = self.mode, list(self.history)
        available = {
            'stream': self.uplink.online and bool(VIDEO_URL),
            'usb': self.usb.present,
            'local': bool(local_videos()),
        }
        return {
            **stats,
            'fps': round(self.fps, 2),
            'inference_ms': round(self.infer_ms, 1),
            'detection_history': history,
            'classes': self.class_names,
            'source': {
                'mode': mode,
                'active': self.active,
                'live': bool(self.source and self.source.live),
                'available': available,
                'errors': dict(self.errors),
                'switches': list(self.switches),
                'catalog': SOURCES,
            },
            'uplink': self.uplink.snapshot(),
            'device': {'label': DEVICE_LABEL, 'model': os.path.basename(MODEL_PATH.rstrip('/'))},
        }


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------
print(f'Loading model from: {MODEL_PATH}')
model = YOLO(MODEL_PATH, task='detect')
warm = model(np.zeros((IMGSZ, IMGSZ, 3), dtype=np.uint8), imgsz=IMGSZ, verbose=False)
print(f'Model ready. Classes: {list(warm[0].names.values())}')

uplink, usb = UplinkMonitor(), UsbMonitor()
uplink.start()
usb.start()
for _ in range(40):  # let the first uplink probe land so auto mode starts on the right source
    if uplink.state != 'unknown':
        break
    time.sleep(0.1)
print(f'Uplink: {uplink.state}, USB camera: {"present" if usb.present else "not found"}, local videos: {len(local_videos())}')
pipeline = Pipeline(model, uplink, usb)
pipeline.class_names = list(warm[0].names.values())
pipeline.start()

app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html', device_label=DEVICE_LABEL)


@app.route('/video_feed')
def video_feed():
    return Response(pipeline.frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/stats')
def stats():
    return jsonify(pipeline.snapshot())


@app.route('/api/source', methods=['POST'])
def set_source():
    mode = (request.get_json(silent=True) or {}).get('mode') or request.form.get('mode')
    if mode not in MODES:
        return jsonify({'error': f'mode must be one of {MODES}'}), 400
    pipeline.set_mode(mode)
    return jsonify(pipeline.snapshot()['source'])


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8000'))
    app.run(host='0.0.0.0', port=port, threaded=True)
