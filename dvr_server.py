# ==================== SECURECAM DVR - RTSP + RESTORED UI + BACKEND FIXES ====================
import cv2
import numpy as np
import threading
import time
import os
import json
import socket
import struct
import netifaces as ni
import ipaddress
from datetime import datetime
from flask import Flask, render_template_string, Response, send_from_directory, request, redirect, url_for, abort, make_response
from functools import wraps
import atexit

app = Flask(__name__)

# === DIRECTORIES ===
CONFIG_FILE = 'config.json'
RECORDINGS_DIR = 'recordings'
SNAPSHOTS_DIR = 'snapshots'
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

# === DEFAULT CONFIG ===
DEFAULT_CONFIG = {
    "password": "admin123",
    "fps": 20,
    "camera_grid_columns": 2,
    "recording_mode": "motion",
    "motion_sensitivity": 5000,
    "motion_post_delay": 10,
    "take_snapshot_on_motion": True,
    "snapshot_min_interval": 5,
    "retention_days": 28, 
    "cameras": []
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg: cfg[k] = v
            return cfg
        except: pass
    return DEFAULT_CONFIG.copy()

config = load_config()
config_lock = threading.Lock()
USERNAME = "admin"

def save_config():
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)

# === 28-DAY STORAGE CLEANUP ===
class StorageCleanupThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.running = True
    def run(self):
        while self.running:
            now = time.time()
            cutoff = now - (config.get('retention_days', 28) * 86400)
            for folder in [RECORDINGS_DIR, SNAPSHOTS_DIR]:
                try:
                    for filename in os.listdir(folder):
                        path = os.path.join(folder, filename)
                        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                            os.remove(path)
                except: pass
            time.sleep(3600) # Check hourly

# === AUTHENTICATION ===
def check_auth(username, password):
    return username == USERNAME and password == config.get('password', 'admin123')

def authenticate():
    return make_response('Auth Required', 401, {'WWW-Authenticate': 'Basic realm="Login"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password): return authenticate()
        return f(*args, **kwargs)
    return decorated

# === CAMERA ENGINE (RTSP ENABLED) ===
class CameraThread(threading.Thread):
    def __init__(self, cam_config, cam_id):
        super().__init__(daemon=True)
        self.cam_id = cam_id
        self.name = cam_config.get('name', 'Cam')
        self.source = cam_config.get('source')
        self.type = cam_config.get('type', 'local')
        self.ip = cam_config.get('ip')
        self.latest_frame = None
        self.recording = False
        self.writer = None
        self.last_motion = 0
        self.prev_gray = None
        self.frame_lock = threading.Lock()
        self.stop_signal = False

    def run(self):
        while not self.stop_signal:
            if self.type == 'securecam':
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(5)
                    s.connect((self.ip, 5554))
                    conn = s.makefile('rb')
                    while not self.stop_signal:
                        header = conn.read(4)
                        if not header: break
                        sz = struct.unpack('>I', header)[0]
                        frame = cv2.imdecode(np.frombuffer(conn.read(sz), np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None: self.process_frame(frame)
                    s.close()
                except: time.sleep(5)
            else:
                # RTSP and Local use VideoCapture
                cap = cv2.VideoCapture(self.source)
                # Optimize for RTSP buffer lag
                if self.type == 'rtsp':
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 3) 
                
                while not self.stop_signal and cap.isOpened():
                    ret, frame = cap.read()
                    if not ret: break
                    self.process_frame(frame)
                cap.release()
                time.sleep(3)

    def process_frame(self, frame):
        h, w = frame.shape[:2]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, ts, (10, h-20), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
        
        # Motion detection logic
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (21,21), 0)
        motion = False
        if self.prev_gray is not None:
            delta = cv2.absdiff(self.prev_gray, gray)
            thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
            contours, _ = cv2.findContours(cv2.dilate(thresh, None, iterations=2), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            motion = any(cv2.contourArea(c) > config['motion_sensitivity'] for c in contours)
            if motion: self.last_motion = time.time()
        
        self.prev_gray = gray
        
        # Determine if we should record
        should_rec = (config['recording_mode'] == "continuous") or (time.time() - self.last_motion < config['motion_post_delay'])
        
        if should_rec:
            if not self.recording:
                fn = f"{self.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webm"
                self.writer = cv2.VideoWriter(os.path.join(RECORDINGS_DIR, fn), cv2.VideoWriter_fourcc(*'VP80'), config['fps'], (w, h))
                self.recording = True
            self.writer.write(frame)
        elif self.recording:
            self.writer.release()
            self.recording = False

        with self.frame_lock:
            _, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            self.latest_frame = jpeg.tobytes()

    def get_frame(self):
        with self.frame_lock: return self.latest_frame

camera_threads = []
def restart_cameras():
    global camera_threads
    for t in camera_threads: t.stop_signal = True
    camera_threads = []
    for i, c in enumerate(config['cameras']):
        t = CameraThread(c, i)
        t.start()
        camera_threads.append(t)

# === WEB ROUTES ===
@app.route('/')
@requires_auth
def index():
    return render_template_string(MAIN_HTML, config=config, cameras=config['cameras'], now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/video_feed/<int:cam_id>')
@requires_auth
def video_feed(cam_id):
    if cam_id >= len(camera_threads): abort(404)
    def gen():
        while True:
            f = camera_threads[cam_id].get_frame()
            if f: yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + f + b'\r\n')
            time.sleep(1/config['fps'])
    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/recordings')
@requires_auth
def recordings():
    files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith('.webm')], reverse=True)
    return render_template_string(RECORDINGS_HTML, files=files)

@app.route('/snapshots')
@requires_auth
def snapshots():
    files = sorted([f for f in os.listdir(SNAPSHOTS_DIR) if f.endswith('.jpg')], reverse=True)
    return render_template_string(SNAPSHOTS_HTML, files=files)

@app.route('/files/<folder>/<filename>')
@requires_auth
def serve_file(folder, filename):
    return send_from_directory(RECORDINGS_DIR if folder=='rec' else SNAPSHOTS_DIR, filename)

@app.route('/admin')
@requires_auth
def admin():
    return render_template_string(ADMIN_HTML, config=config, enumerate=enumerate)

@app.route('/admin/save_general', methods=['POST'])
@requires_auth
def save_general():
    with config_lock:
        config['password'] = request.form['password']
        config['fps'] = int(request.form['fps'])
        config['camera_grid_columns'] = int(request.form['camera_grid_columns'])
        config['motion_sensitivity'] = int(request.form['motion_sensitivity'])
        config['motion_post_delay'] = int(request.form['motion_post_delay'])
        save_config()
    return redirect('/admin')

@app.route('/admin/add_cam', methods=['POST'])
@requires_auth
def add_cam():
    name, typ, src = request.form['name'], request.form['type'], request.form['src']
    new_cam = {"name": name, "type": typ}
    if typ == "securecam": 
        new_cam["ip"] = src
    else: 
        # For local, convert to int; for RTSP, keep as string URL
        new_cam["source"] = int(src) if src.isdigit() else src
    with config_lock:
        config['cameras'].append(new_cam)
        save_config()
    restart_cameras()
    return redirect('/admin')

@app.route('/admin/del_cam/<int:i>', methods=['POST'])
@requires_auth
def del_cam(i):
    with config_lock:
        if 0 <= i < len(config['cameras']): del config['cameras'][i]
        save_config()
    restart_cameras()
    return redirect('/admin')

# === HTML TEMPLATES (RTSP UI RESTORED) ===
MAIN_HTML = """
<!DOCTYPE html><html><head><title>SecureCam DVR</title>
<style>
    body{background:#121212;color:#fff;font-family:sans-serif;margin:0;text-align:center;}
    nav{background:#1e1e1e;padding:15px;} nav a{color:#0f0;margin:0 15px;text-decoration:none;font-weight:bold;}
    .grid{display:grid;grid-template-columns:repeat({{config.camera_grid_columns}},1fr);gap:20px;padding:20px;}
    img{width:100%;border-radius:12px;border:3px solid #333;box-shadow:0 10px 20px rgba(0,0,0,0.5);}
    .cam-card{background:#1e1e1e;padding:10px;border-radius:12px;}
</style></head>
<body><h1>🔒 SecureCam DVR</h1><nav><a href="/">Live</a>|<a href="/recordings">Recordings</a>|<a href="/snapshots">Snapshots</a>|<a href="/admin">Admin</a></nav>
<div class="grid">{% for c in cameras %}<div class="cam-card"><h3>{{c.name}}</h3><img src="{{url_for('video_feed',cam_id=loop.index0)}}"></div>{% endfor %}</div>
<div style="color:#666;margin-bottom:20px;">Retention: {{config.retention_days}} Days | {{now}}</div></body></html>"""

RECORDINGS_HTML = """
<!DOCTYPE html><html><body style="background:#121212;color:#fff;text-align:center;font-family:sans-serif;">
<h1>Recordings (28-Day Retention)</h1><a href="/" style="color:#0f0;text-decoration:none;">← Back</a><hr>
{% for f in files %}<div style="margin-bottom:40px;"><h3>{{f}}</h3><video controls width="720" style="border-radius:10px;"><source src="/files/rec/{{f}}" type="video/webm"></video></div>{% endfor %}
</body></html>"""

SNAPSHOTS_HTML = """
<!DOCTYPE html><html><body style="background:#121212;color:#fff;text-align:center;font-family:sans-serif;">
<h1>Snapshots</h1><a href="/" style="color:#0f0;text-decoration:none;">← Back</a><hr>
<div style="display:flex; flex-wrap:wrap; justify-content:center;">
{% for f in files %}<div style="margin:15px;background:#1e1e1e;padding:10px;border-radius:8px;"><img src="/files/snap/{{f}}" width="350" style="border-radius:5px;"><br><small>{{f}}</small></div>{% endfor %}
</div></body></html>"""

ADMIN_HTML = """
<!DOCTYPE html><html><head><title>Admin Panel</title>
<style>
    body{background:#121212;color:#fff;font-family:sans-serif;padding:20px;}
    .card{background:#1e1e1e;padding:20px;border-radius:12px;margin-bottom:20px;max-width:700px;margin-left:auto;margin-right:auto;box-shadow:0 4px 15px rgba(0,0,0,0.4);}
    input, select{width:95%;padding:10px;margin:10px 0;background:#2a2a2a;color:#fff;border:1px solid #444;border-radius:5px;}
    button{background:#0f0;color:#000;border:none;padding:12px;width:100%;font-weight:bold;cursor:pointer;border-radius:5px;transition:0.2s;}
    button:hover{background:#0c0;transform:scale(1.01);}
    table{width:100%;margin-top:15px;border-collapse:collapse;} td,th{padding:10px;text-align:left;border-bottom:1px solid #333;}
</style></head>
<body><h1 style="text-align:center;color:#0f0;">Admin Settings</h1><p style="text-align:center;"><a href="/" style="color:#0f0;text-decoration:none;">← Back to Live View</a></p>
<div class="card">
    <h2>General Settings</h2>
    <form action="/admin/save_general" method="post">
        <label>System Password</label><input type="text" name="password" value="{{config.password}}">
        <label>Streaming FPS</label><input type="number" name="fps" value="{{config.fps}}">
        <label>Grid Columns</label><input type="number" name="camera_grid_columns" value="{{config.camera_grid_columns}}">
        <label>Motion Sensitivity (Lower = More Sensitive)</label><input type="number" name="motion_sensitivity" value="{{config.motion_sensitivity}}">
        <label>Post-Motion Delay (Seconds)</label><input type="number" name="motion_post_delay" value="{{config.motion_post_delay}}">
        <button type="submit">Update Core Settings</button>
    </form>
</div>
<div class="card">
    <h2>Camera Management</h2>
    <table>
        <tr><th>Name</th><th>Type</th><th>Action</th></tr>
        {% for i, c in enumerate(config.cameras) %}
        <tr><td>{{c.name}}</td><td>{{c.type}}</td><td>
            <form action="/admin/del_cam/{{i}}" method="post"><button style="background:#f44;padding:5px 10px;width:auto;">Remove</button></form>
        </td></tr>
        {% endfor %}
    </table>
    <hr style="border:0;border-top:1px solid #444;margin:20px 0;">
    <h3>Add New Stream</h3>
    <form action="/admin/add_cam" method="post">
        <input type="text" name="name" placeholder="Friendly Camera Name" required>
        <select name="type">
            <option value="local">Local Webcam (USB)</option>
            <option value="rtsp">RTSP / IP Camera Stream</option>
            <option value="securecam">SecureCam Client (LAN)</option>
        </select>
        <input type="text" name="src" placeholder="Source: (e.g. 0 or rtsp://admin:pass@192.168.1.50/stream)" required>
        <button type="submit">Add Camera Source</button>
    </form>
</div>
<div class="card" style="text-align:center;border:1px dashed #0f0;">
    <p>Storage Policy: <strong>28-Day Auto-Cleanup</strong> is active.</p>
</div>
</body></html>"""

if __name__ == '__main__':
    StorageCleanupThread().start()
    restart_cameras()
    app.run(host='0.0.0.0', port=5000, threaded=True)
