
import cv2
import glob
import os
from ultralytics import YOLO

# Load the YOLO model
# model = YOLO('PPE-yolov8s.pt')
model = YOLO('bestn.pt')  # PPE Yolo11

print("Model classes:", model.names)

def play_videos_in_loop(video_files):
    while True:
        for video_path in video_files:
            print(f"Playing: {video_path}")
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                print(f"Cannot open video: {video_path}")
                continue
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                results = model(frame, verbose=False)
                annotated_frame = frame.copy()
                boxes = results[0].boxes
                names = model.names
                for box in boxes:
                    conf = float(box.conf[0]) if hasattr(box, 'conf') else 1.0
                    cls = int(box.cls[0]) if hasattr(box, 'cls') else -1
                    label = names.get(cls, str(cls))
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    # Only apply red/green for 'hardhat' class
                    if label.lower() == 'hardhat':
                        color = (0, 0, 255) if conf < 0.75 else (0, 255, 0)
                    else:
                        color = (0, 255, 0)
                    cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
                    text = f"{label} {conf:.2f}"
                    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                    cv2.rectangle(annotated_frame, (x1, y1 - th - 4), (x1 + tw, y1), color, -1)
                    cv2.putText(annotated_frame, text, (x1, y1 - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)
                cv2.imshow('YOLO PPE Detection', annotated_frame)
                # Press 'q' to exit the whole loop
                if cv2.waitKey(1) == ord('q'):
                    cap.release()
                    cv2.destroyAllWindows()
                    return
            cap.release()

    cv2.destroyAllWindows()

# Find all mp4 files in the current directory
mp4_files = glob.glob(os.path.join(os.getcwd(), 'sample-videos/*.mp4'))
if not mp4_files:
    print("No mp4 files found in the current directory.")
else:
    play_videos_in_loop(mp4_files)