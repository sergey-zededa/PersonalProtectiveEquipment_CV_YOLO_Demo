"""Userspace UVC camera capture through libuvc (ctypes).

EVE-OS runs container apps inside a lightweight VM that boots the EVE kernel,
which ships without the uvcvideo driver, so a passed-through USB camera never
shows up as /dev/video*. libuvc talks to the camera directly over libusb
(/dev/bus/usb), which needs no kernel driver at all.

Only the MJPEG path is used: the camera compresses, we decode with OpenCV.
"""
import ctypes
import ctypes.util
import os
import select
import struct
import subprocess
import sys

import cv2
import numpy as np

UVC_FRAME_FORMAT_MJPEG = 7
UVC_ERROR_TIMEOUT = -7


class _UvcFrame(ctypes.Structure):
    # Leading fields of struct uvc_frame; the rest is never touched.
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("data_bytes", ctypes.c_size_t),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
    ]


def _load():
    name = ctypes.util.find_library("uvc") or "libuvc.so.0"
    try:
        lib = ctypes.CDLL(name)
    except OSError:
        return None
    vp, pvp = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
    lib.uvc_init.argtypes = [pvp, vp]
    lib.uvc_exit.argtypes = [vp]
    lib.uvc_find_device.argtypes = [vp, pvp, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
    lib.uvc_open.argtypes = [vp, pvp]
    lib.uvc_close.argtypes = [vp]
    lib.uvc_unref_device.argtypes = [vp]
    lib.uvc_get_stream_ctrl_format_size.argtypes = [vp, vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lib.uvc_stream_open_ctrl.argtypes = [vp, pvp, vp]
    lib.uvc_stream_start.argtypes = [vp, vp, vp, ctypes.c_uint8]
    lib.uvc_stream_get_frame.argtypes = [vp, ctypes.POINTER(ctypes.POINTER(_UvcFrame)), ctypes.c_int32]
    lib.uvc_stream_stop.argtypes = [vp]
    lib.uvc_stream_close.argtypes = [vp]
    lib.uvc_strerror.argtypes = [ctypes.c_int]
    lib.uvc_strerror.restype = ctypes.c_char_p
    return lib


_lib = _load()


def available():
    return _lib is not None


def _check(rc, what):
    if rc < 0:
        raise RuntimeError(f"{what}: {_lib.uvc_strerror(rc).decode()}")


def camera_present():
    """True if libuvc can see a UVC camera (does not open it)."""
    if _lib is None:
        return False
    ctx, dev = ctypes.c_void_p(), ctypes.c_void_p()
    if _lib.uvc_init(ctypes.byref(ctx), None) < 0:
        return False
    try:
        found = _lib.uvc_find_device(ctx, ctypes.byref(dev), 0, 0, None) >= 0
        if found:
            _lib.uvc_unref_device(dev)
        return found
    finally:
        _lib.uvc_exit(ctx)


class UvcCamera:
    """First UVC camera found, streaming MJPEG at the closest supported mode."""

    MODES = [(1280, 720, 30), (1024, 576, 30), (640, 480, 30), (640, 360, 30)]

    def __init__(self, width=1280, height=720, fps=30):
        if _lib is None:
            raise RuntimeError("libuvc is not installed")
        self.ctx, self.dev, self.devh, self.strmh = (ctypes.c_void_p() for _ in range(4))
        self.ctrl = ctypes.create_string_buffer(256)  # uvc_stream_ctrl_t is ~48 bytes
        try:
            _check(_lib.uvc_init(ctypes.byref(self.ctx), None), "uvc_init")
            _check(_lib.uvc_find_device(self.ctx, ctypes.byref(self.dev), 0, 0, None), "find camera")
            _check(_lib.uvc_open(self.dev, ctypes.byref(self.devh)), "open camera")
            modes = [(width, height, fps)] + [m for m in self.MODES if m != (width, height, fps)]
            for w, h, f in modes:
                if _lib.uvc_get_stream_ctrl_format_size(self.devh, self.ctrl, UVC_FRAME_FORMAT_MJPEG, w, h, f) >= 0:
                    self.mode = (w, h, f)
                    break
            else:
                raise RuntimeError("camera offers no usable MJPEG mode")
            _check(_lib.uvc_stream_open_ctrl(self.devh, ctypes.byref(self.strmh), self.ctrl), "open stream")
            _check(_lib.uvc_stream_start(self.strmh, None, None, 0), "start stream")
        except Exception:
            self.close()
            raise

    def read(self, timeout_s=2.0):
        """Return the newest BGR frame, or None on timeout/decode error."""
        return self.decode(self.read_raw(timeout_s))

    @staticmethod
    def decode(jpeg):
        if jpeg is None:
            return None
        return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)

    def read_raw(self, timeout_s=2.0):
        """Return the newest frame as undecoded MJPEG bytes, or None on timeout."""
        frame = ctypes.POINTER(_UvcFrame)()
        rc = _lib.uvc_stream_get_frame(self.strmh, ctypes.byref(frame), int(timeout_s * 1e6))
        if rc == UVC_ERROR_TIMEOUT or not frame:
            return None
        _check(rc, "get frame")
        f = frame.contents
        if not f.data or f.data_bytes == 0:
            return None
        return ctypes.string_at(f.data, f.data_bytes)

    def close(self):
        if self.strmh:
            _lib.uvc_stream_stop(self.strmh)
            _lib.uvc_stream_close(self.strmh)
            self.strmh = ctypes.c_void_p()
        if self.devh:
            _lib.uvc_close(self.devh)
            self.devh = ctypes.c_void_p()
        if self.dev:
            _lib.uvc_unref_device(self.dev)
            self.dev = ctypes.c_void_p()
        if self.ctx:
            _lib.uvc_exit(self.ctx)
            self.ctx = ctypes.c_void_p()


class UvcProcessCamera:
    """UvcCamera running in a child process.

    When a camera is unplugged or knocked mid-stream, libuvc's stop call can hang and its
    handle keeps the USB interface claimed, so every later open fails with "Busy" until the
    app restarts. Killing a child process always makes the kernel release the claim.
    Frames come over the child's stdout as <4-byte big-endian length><MJPEG bytes>.
    """

    def __init__(self, width=1280, height=720, fps=30, open_timeout_s=15):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), str(width), str(height), str(fps)],
            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0)  # unbuffered: select() must see all data
        self.out = self.proc.stdout
        header = self._read_line(open_timeout_s)
        if not header or not header.startswith(b'OK '):
            msg = header[4:].decode(errors='replace').strip() if header else 'camera process did not start'
            self.close()
            raise RuntimeError(msg or 'camera process failed')
        self.mode = tuple(int(v) for v in header.split()[1:4])

    def _wait(self, timeout_s):
        return bool(select.select([self.out], [], [], timeout_s)[0])

    def _read_line(self, timeout_s):
        if not self._wait(timeout_s):
            return None
        return self.out.readline()

    def _read_exact(self, n):
        buf = b''
        while len(buf) < n:
            chunk = self.out.read(n - len(buf))
            if not chunk:
                raise RuntimeError('camera process exited')
            buf += chunk
        return buf

    def read_raw(self, timeout_s=2.0):
        if not self._wait(timeout_s):
            return None
        (n,) = struct.unpack('>I', self._read_exact(4))
        return self._read_exact(n)

    decode = staticmethod(UvcCamera.decode)

    def read(self, timeout_s=2.0):
        return self.decode(self.read_raw(timeout_s))

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.out.close()


def _serve(width, height, fps):
    """Child side of UvcProcessCamera: stream frames to stdout until the camera stalls."""
    out = sys.stdout.buffer
    try:
        cam = UvcCamera(width, height, fps)
    except Exception as e:  # noqa: BLE001 - report any open failure to the parent
        out.write(b'ERR ' + str(e).replace('\n', ' ').encode() + b'\n')
        out.flush()
        return 1
    out.write('OK {} {} {}\n'.format(*cam.mode).encode())
    out.flush()
    misses = 0
    while misses < 3:  # ~6 s without a frame: exit and let the parent reopen
        jpeg = cam.read_raw(2.0)
        if jpeg is None:
            misses += 1
            continue
        misses = 0
        try:
            out.write(struct.pack('>I', len(jpeg)) + jpeg)
            out.flush()
        except BrokenPipeError:
            break
    return 0  # exit without cam.close(): the kernel releases the device either way


if __name__ == '__main__':
    sys.exit(_serve(*(int(v) for v in sys.argv[1:4])))
