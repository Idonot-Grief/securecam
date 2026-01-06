# ==================== SECURECAM CLIENT (CROSS-PLATFORM) ====================
import cv2
import socket
import struct
import threading
import time
import json
import os
import platform

# === CONFIGURATION ===
CONFIG_FILE = 'securecam_config.json'
DISCOVERY_PORT = 5552
VIDEO_PORT = 5554
BROADCAST_IP = '<broadcast>'

DEFAULT_CONFIG = {
    "camera_name": "SecureCam-01",
    "camera_index": 0,
    "discovery_enabled": True,
    "jpeg_quality": 85,
    "resolution": [1280, 720],
    "fps": 20
}

# === CONFIG LOADER ===
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                user_cfg = json.load(f)
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(user_cfg)
            return cfg
        except Exception as e:
            print(f"Config load error: {e}, using defaults.")

    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_CONFIG, f, indent=4)
    print(f"Created default {CONFIG_FILE}")
    return DEFAULT_CONFIG.copy()

config = load_config()

# === CAMERA OPEN (PLATFORM SAFE) ===
def open_camera():
    cam_index = config.get("camera_index", 0)

    if platform.system() == "Windows":
        backend = cv2.CAP_DSHOW
    else:
        backend = cv2.CAP_V4L2

    cap = cv2.VideoCapture(cam_index, backend)

    if isinstance(cam_index, int):
        if 'resolution' in config:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config['resolution'][0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config['resolution'][1])
        cap.set(cv2.CAP_PROP_FPS, config.get('fps', 20))

    return cap

# === DISCOVERY BROADCAST ===
def broadcast_name():
    if not config.get('discovery_enabled', True):
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = config['camera_name'].encode('utf-8')

    print(f"Broadcasting '{config['camera_name']}'")

    while True:
        try:
            sock.sendto(msg, (BROADCAST_IP, DISCOVERY_PORT))
        except:
            pass
        time.sleep(5)

# === VIDEO SERVER ===
def video_server():
    print("\n=== SecureCam Client Started ===")
    print(f"Name   : {config['camera_name']}")
    print(f"Source : {config['camera_index']}")
    print(f"Port   : {VIDEO_PORT}")
    print(f"OS     : {platform.system()}")
    print("================================\n")

    if config.get('discovery_enabled', True):
        threading.Thread(target=broadcast_name, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind(('', VIDEO_PORT))
        server.listen(5)
        print(f"Listening on port {VIDEO_PORT}")
    except Exception as e:
        print(f"Bind failed: {e}")
        return

    while True:
        conn, addr = server.accept()
        print(f"\nDVR connected: {addr}")

        cap = None
        while cap is None or not cap.isOpened():
            cap = open_camera()
            if not cap.isOpened():
                print("Camera open failed, retrying...")
                time.sleep(3)

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    print("Frame read failed, reopening camera...")
                    cap.release()
                    time.sleep(1)
                    cap = open_camera()
                    continue

                if 'resolution' in config:
                    frame = cv2.resize(frame, tuple(config['resolution']))

                ret, jpeg = cv2.imencode(
                    '.jpg',
                    frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), config.get('jpeg_quality', 85)]
                )

                if not ret:
                    continue

                data = jpeg.tobytes()
                conn.sendall(struct.pack('>I', len(data)) + data)
                time.sleep(1 / config.get('fps', 20))

        except Exception as e:
            print(f"Streaming error: {e}")

        finally:
            conn.close()
            cap.release()
            print("Connection closed\n")

if __name__ == "__main__":
    video_server()
