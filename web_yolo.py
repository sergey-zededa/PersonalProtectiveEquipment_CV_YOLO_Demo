
import cv2
import threading
import time
import os
from collections import defaultdict, deque
from flask import Flask, Response, render_template, jsonify
from ultralytics import YOLO
from datetime import datetime

app = Flask(__name__)
# Allow overriding model path via env var
MODEL_PATH = os.environ.get('MODEL_PATH', 'bestn.pt')
print(f"Loading model from: {MODEL_PATH}")
model = YOLO(MODEL_PATH)
print(f"Model loaded successfully. Model names: {list(model.names.values())}")

VIDEO_URL = os.environ.get('CAMERA_STREAM_URL', 'http://192.168.0.190/videos/playlist.m3u8')  # fallback to previous default

# Shared stats storage
stats_lock = threading.Lock()
current_stats = {
    "detections_per_class": defaultdict(int),
    "confidence_scores": defaultdict(list),
    "fps": 0.0,
    "last_detection_time": "",
    "detection_history": deque(maxlen=20)  # store last 20 frames
}

def update_stats(results, frame_start, frame_end):
    names = model.names
    detections = defaultdict(int)
    confidences = defaultdict(list)
    detection_list = []

    for box in results[0].boxes:
        conf = float(box.conf[0]) if hasattr(box, 'conf') else 1.0
        cls = int(box.cls[0]) if hasattr(box, 'cls') else -1
        label = names.get(cls, str(cls))
        detections[label] += 1
        confidences[label].append(conf)
        detection_list.append({
            "label": label,
            "confidence": conf
        })

    with stats_lock:
        current_stats["detections_per_class"] = dict(detections)
        current_stats["confidence_scores"] = {
            label: {
                "avg": sum(scores)/len(scores) if scores else 0,
                "min": min(scores) if scores else 0,
                "max": max(scores) if scores else 0,
                "all": scores
            }
            for label, scores in confidences.items()
        }
        # FPS calculation
        elapsed = frame_end - frame_start
        current_stats["fps"] = 1.0 / elapsed if elapsed > 0 else 0.0
        current_stats["last_detection_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_stats["detection_history"].append({
            "timestamp": current_stats["last_detection_time"],
            "detections": detection_list
        })

def gen_frames():
    names = model.names
    while True:
        cap = cv2.VideoCapture(VIDEO_URL)
        while cap.isOpened():
            frame_start = time.time()
            success, frame = cap.read()
            if not success:
                break
            results = model(frame, verbose=False)
            annotated_frame = frame.copy()
            for box in results[0].boxes:
                conf = float(box.conf[0]) if hasattr(box, 'conf') else 1.0
                cls = int(box.cls[0]) if hasattr(box, 'cls') else -1
                label = names.get(cls, str(cls))
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                # Only apply red/green for 'hardhat' class
                if label.lower() == 'hardhat':
                    color = (0, 0, 255) if conf < 0.6 else (0, 255, 0)
                else:
                    color = (0, 255, 0)
                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
                text = f"{label} {conf:.2f}"
                (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                cv2.rectangle(annotated_frame, (x1, y1 - th - 4), (x1 + tw, y1), color, -1)
                cv2.putText(annotated_frame, text, (x1, y1 - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
            frame_end = time.time()
            update_stats(results, frame_start, frame_end)
            ret, buffer = cv2.imencode('.jpg', annotated_frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        cap.release()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(gen_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stats')
def stats():
    with stats_lock:
        stats_copy = {
            "detections_per_class": dict(current_stats["detections_per_class"]),
            "confidence_scores": current_stats["confidence_scores"],
            "fps": current_stats["fps"],
            "last_detection_time": current_stats["last_detection_time"],
            "detection_history": list(current_stats["detection_history"])
        }
    return jsonify(stats_copy)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port)
