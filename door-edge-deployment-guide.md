# Door-Edge Deployment Guide — New Machine

## 1. Transfer the project
```bash
unzip Door-Edge.zip -d ~/Door-Edge
cd ~/Door-Edge
```

## 2. Install system dependencies
```bash
sudo apt update
sudo apt install -y \
    python3 python3-venv python3-pip \
    libgl1-mesa-glx libglib2.0-0 \
    v4l-utils git
```

## 3. Camera access
```bash
ls /dev/video*                  # confirm the camera shows up
sudo usermod -aG video $USER    # let your user access it without sudo
```
Log out and back in for the group change to apply (or `newgrp video` for the current shell).

## 4. Fix the Windows-specific code
Two edits are required — the code as-is won't run on Linux.

**`config.py`** — replace the three hardcoded `D:\...` paths:
```python
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

INSIGHTFACE_MODEL_DIR = os.path.join(BASE_DIR, "baffolo_sc_openvino_model")
YOLO_MODEL_PATH       = os.path.join(BASE_DIR, "Antispoofing_openvino_model")
HAND_MODEL_PATH       = os.path.join(BASE_DIR, "hand_detection_openvino_model")
```
Also check `REGISTRATION_DB_PATH` — confirm the sibling folder it expects actually exists on this machine, or update the path.

**`core/camera_worker.py`** — find this line:
```python
cap = cv2.VideoCapture(cfg.CAMERA_INDEX, cv2.CAP_DSHOW)
```
Change to:
```python
cap = cv2.VideoCapture(cfg.CAMERA_INDEX, cv2.CAP_V4L2)
```

## 5. Start the broker (Mosquitto, via Docker)
```bash
sudo apt install -y docker.io docker-compose-plugin
cd ~/Door-Edge
docker compose up -d mosquitto
docker ps    # confirm door-mosquitto is running
```

## 6. Confirm Redis is reachable
Door-Edge shares Redis with FaceTrack — it doesn't start its own. Make sure that instance is up first:
```bash
redis-cli -h localhost -p 6379 ping   # should return PONG
```
If that fails, start/point at whichever machine runs `facetrack_redis` before continuing.

## 7. Set up the Python environment
```bash
cd ~/Door-Edge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 8. Sanity-check imports before the full run
```bash
python -c "import openvino, cv2, insightface, ultralytics; print('ok')"
```
If OpenVINO complains about missing shared libraries here, it's usually a missing apt package — the error message will name it.

## 9. Test the camera standalone
```bash
python -c "import cv2; c=cv2.VideoCapture(0); print(c.read()[0])"
```
Should print `True`.

## 10. Run it
```bash
python edge_app.py
```
Watch the startup log — it should report models loaded, MQTT connected, and a sync count (`N face(s) enrolled in Redis`). If enrolled is `0`, check step 4's `REGISTRATION_DB_PATH`.

## 11. Verify MQTT is actually flowing
In a second terminal:
```bash
mosquitto_sub -h localhost -t 'door/#' -v
```
Walk in front of the camera — you should see `door/health`, and then `door/cmd/unlock` or `door/access/denied` as recognition happens.
