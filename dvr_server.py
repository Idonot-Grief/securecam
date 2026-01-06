# ==================== MAIN DVR SERVER (app.py) - FIXED JSON LOAD + FULL CODE ====================
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
    "auto_recording": True,
    "recording_mode": "motion",
    "use_schedules": False,
    "schedules": [],
    "motion_sensitivity": 5000,
    "motion_post_delay": 10,
    "take_snapshot_on_motion": True,
    "snapshot_min_interval": 5,
    "enable_securecam_discovery": True,
    "securecam_discovery_port": 5552,
    "securecam_video_port": 5554,
    "cameras": []
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            # Fixed: Use UTF-8 encoding to avoid charmap errors
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            # Backward compatibility - add missing keys
            for key, val in DEFAULT_CONFIG.items():
                if key not in cfg:
                    cfg[key] = val
            return cfg
        except Exception as e:
            print(f"Error loading config.json: {e}")
            print("Creating a fresh default config.json...")
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
            return DEFAULT_CONFIG
    else:
        print("No config.json found. Creating default...")
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG

config = load_config()
config_lock = threading.Lock()

USERNAME = "admin"

# === AUTHENTICATION ===
def check_auth(username, password):
    return username == USERNAME and password == config.get('password', 'admin123')

def authenticate():
    return make_response('Authentication required!', 401, {'WWW-Authenticate': 'Basic realm="SecureCam DVR"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# === LOCAL NETWORK DETECTION ===
def get_local_network():
    try:
        gateways = ni.gateways()
        default_gw = gateways['default'][ni.AF_INET]
        iface = default_gw[1]
        addr = ni.ifaddresses(iface)[ni.AF_INET][0]
        ip = addr['addr']
        netmask = addr['netmask']
        network = ipaddress.ip_network(f"{ip}/{netmask}", strict=False)
        return str(network)
    except Exception as e:
        print(f"Could not detect local network: {e}")
        return None

# === LAN SCAN THREAD FOR SECURECAM ===
class LanScanThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.running = True

    def run(self):
        if not config.get('enable_securecam_discovery', True):
            print("SecureCam LAN scan disabled.")
            return

        network_str = get_local_network()
        if not network_str:
            print("Could not determine local network for scanning.")
            return

        print(f"Starting LAN scan on network: {network_str} (port {config['securecam_video_port']})")

        while self.running:
            try:
                for host in ipaddress.ip_network(network_str).hosts():
                    ip_str = str(host)
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.3)
                    try:
                        result = sock.connect_ex((ip_str, config['securecam_video_port']))
                        if result == 0:
                            self.try_add_securecam(ip_str)
                    finally:
                        sock.close()
            except Exception as e:
                print(f"LAN scan error: {e}")
            time.sleep(60)  # Scan every minute

    def try_add_securecam(self, ip):
        with config_lock:
            for cam in config['cameras']:
                if cam.get('type') == 'securecam' and cam.get('ip') == ip:
                    return
            new_cam = {
                "type": "securecam",
                "name": f"SecureCam-{ip.split('.')[-1]}",
                "ip": ip,
                "video_port": config['securecam_video_port']
            }
            config['cameras'].append(new_cam)
            print(f"+++ LAN scan discovered SecureCam at {ip}")
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4)
                restart_cameras()
            except Exception as e:
                print(f"Failed to save config: {e}")

    def stop(self):
        self.running = False

lan_scan_thread = LanScanThread()
if config.get('enable_securecam_discovery', True):
    lan_scan_thread.start()

def cleanup():
    if 'lan_scan_thread' in globals():
        lan_scan_thread.stop()
        lan_scan_thread.join(timeout=2)

atexit.register(cleanup)

# === CAMERA THREAD (unchanged) ===
class CameraThread(threading.Thread):
    def __init__(self, cam_config, cam_id):
        super().__init__(daemon=True)
        self.cam_id = cam_id
        self.config = cam_config
        self.name = cam_config.get('name', f'Cam {cam_id + 1}')
        self.type = cam_config.get('type', 'local')
        self.source = cam_config.get('source') if 'source' in cam_config else None
        self.ip = cam_config.get('ip')
        self.video_port = cam_config.get('video_port', 5554)
        self.cap = None
        self.latest_frame = None
        self.recording = False
        self.writer = None
        self.last_motion_time = 0
        self.last_snapshot_time = 0
        self.frame_lock = threading.Lock()

    def run(self):
        while True:
            if self.type == 'securecam':
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(10)
                    sock.connect((self.ip, self.video_port))
                    file_conn = sock.makefile('rb')
                    print(f"Connected to SecureCam: {self.name} ({self.ip}:{self.video_port})")
                except Exception as e:
                    print(f"SecureCam connect failed ({self.name}): {e}. Retrying...")
                    time.sleep(5)
                    continue

                while True:
                    try:
                        size_data = file_conn.read(4)
                        if len(size_data) < 4:
                            break
                        frame_size = struct.unpack('>I', size_data)[0]
                        jpeg_data = b''
                        while len(jpeg_data) < frame_size:
                            packet = file_conn.read(frame_size - len(jpeg_data))
                            if not packet:
                                break
                            jpeg_data += packet
                        if len(jpeg_data) != frame_size:
                            break
                        frame = cv2.imdecode(np.frombuffer(jpeg_data, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is None:
                            continue
                    except:
                        break
                    self.process_frame(frame)

                sock.close()
                time.sleep(3)

            else:
                if self.source is not None:
                    if isinstance(self.source, int):
                        self.cap = cv2.VideoCapture(self.source)
                    else:
                        self.cap = cv2.VideoCapture(self.source)
                        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if not self.cap.isOpened():
                        time.sleep(3)
                        continue

                prev_gray = None
                while True:
                    if not self.cap.isOpened():
                        time.sleep(0.1)
                        continue
                    ret, frame = self.cap.read()
                    if not ret:
                        time.sleep(0.1)
                        continue
                    self.process_frame(frame, prev_gray=prev_gray)
                    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    def process_frame(self, frame, prev_gray=None):
        h, w = frame.shape[:2]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, ts, (10, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

        motion_detected = False
        if prev_gray is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            delta = cv2.absdiff(prev_gray, gray)
            thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            motion_detected = any(cv2.contourArea(c) > config['motion_sensitivity'] for c in contours)
            if motion_detected:
                self.last_motion_time = time.time()
                if (config.get('take_snapshot_on_motion', True) and
                    time.time() - self.last_snapshot_time >= config.get('snapshot_min_interval', 5)):
                    self.save_snapshot(frame.copy())
                    self.last_snapshot_time = time.time()

        should_record = self.should_record(motion_detected)

        if should_record and not self.recording:
            filename = f"{self.name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webm"
            path = os.path.join(RECORDINGS_DIR, filename)
            fourcc = cv2.VideoWriter_fourcc(*'VP80')
            self.writer = cv2.VideoWriter(path, fourcc, config['fps'], (w, h))
            self.recording = True
            print(f"[REC START] {filename}")

        if should_record and self.recording:
            self.writer.write(frame)

        if not should_record and self.recording:
            self.writer.release()
            self.writer = None
            self.recording = False
            print(f"[REC STOP] {self.name}")

        with self.frame_lock:
            ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ret:
                self.latest_frame = jpeg.tobytes()

    def should_record(self, motion_detected):
        now = datetime.now()
        if config['use_schedules']:
            weekday = now.weekday()
            time_str = now.strftime("%H:%M")
            in_schedule = any(weekday in s['days'] and s['start'] <= time_str < s['end'] for s in config['schedules'])
            if not in_schedule:
                return False
            return config['recording_mode'] == "continuous" or \
                   (config['recording_mode'] == "motion" and (motion_detected or time.time() - self.last_motion_time < config['motion_post_delay']))
        else:
            if not config['auto_recording']:
                return False
            return config['recording_mode'] == "continuous" or \
                   (config['recording_mode'] == "motion" and (motion_detected or time.time() - self.last_motion_time < config['motion_post_delay']))

    def save_snapshot(self, frame):
        filename = f"{self.name.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        cv2.imwrite(os.path.join(SNAPSHOTS_DIR, filename), frame)

    def get_frame(self):
        with self.frame_lock:
            return self.latest_frame

camera_threads = []

def restart_cameras():
    global camera_threads
    for t in camera_threads:
        if hasattr(t, 'cap') and t.cap and t.cap.isOpened():
            t.cap.release()
    camera_threads = []
    for i, cam_cfg in enumerate(config['cameras']):
        thread = CameraThread(cam_cfg, i)
        thread.start()
        camera_threads.append(thread)
    print(f"Restarted {len(camera_threads)} camera(s)")

restart_cameras()

# === STREAMING ===
def generate_stream(cam_id):
    while True:
        frame = camera_threads[cam_id].get_frame()
        if frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
        time.sleep(1 / config['fps'])

@app.route('/video_feed/<int:cam_id>')
@requires_auth
def video_feed(cam_id):
    if cam_id >= len(camera_threads):
        abort(404)
    return Response(generate_stream(cam_id), mimetype='multipart/x-mixed-replace; boundary=frame')

# === MAIN PAGE ===
@app.route('/')
@requires_auth
def index():
    HTML = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>SecureCam DVR</title>
        <style>
            body { background:#121212; color:white; font-family:Arial; text-align:center; margin:0; }
            h1 { padding:20px; }
            nav { background:#1e1e1e; padding:15px; }
            nav a { color:#00ff99; margin:0 20px; text-decoration:none; font-weight:bold; }
            .grid { display:grid; grid-template-columns:repeat({{ cols }}, 1fr); gap:20px; padding:20px; }
            .cam { text-align:center; }
            img { width:100%; border:3px solid #444; border-radius:10px; }
            .footer { color:#888; padding:15px; }
        </style>
    </head>
    <body>
        <h1>🔒 SecureCam DVR</h1>
        <nav>
            <a href="/">Live View</a> |
            <a href="/recordings">Recordings</a> |
            <a href="/snapshots">Motion Snapshots</a> |
            <a href="/admin">Admin Panel</a>
        </nav>
        {% if cameras %}
        <div class="grid">
            {% for cam in cameras %}
            <div class="cam">
                <h3>{{ cam.name }}</h3>
                <img src="{{ url_for('video_feed', cam_id=loop.index0) }}">
            </div>
            {% endfor %}
        </div>
        {% else %}
        <p>No cameras configured. Go to <a href="/admin">Admin Panel</a> to add them.</p>
        {% endif %}
        <div class="footer">Time: {{ now }}</div>
    </body>
    </html>
    """
    return render_template_string(HTML,
                                 now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 cols=config['camera_grid_columns'],
                                 cameras=config['cameras'])

# === RECORDINGS & SNAPSHOTS (unchanged) ===
@app.route('/recordings')
@requires_auth
def recordings_gallery():
    files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith('.webm')], reverse=True)
    if not files: return "<h1>Recordings</h1><p>No recordings yet.</p><a href='/'>Back</a>"
    html = "<h1>Recordings Gallery</h1><nav><a href='/'>← Back</a></nav><br>"
    for f in files:
        p = url_for('serve_recording', filename=f)
        html += f"<div style='margin:40px auto;max-width:1000px;text-align:center;'><h3>{f}</h3><video controls style='width:100%;max-height:600px;background:#000;border-radius:8px;'><source src='{p}' type='video/webm'></video><br><a href='{p}' download style='color:#00ff99;'>Download</a></div><hr>"
    return html

@app.route('/recordings/<filename>')
@requires_auth
def serve_recording(filename):
    return send_from_directory(RECORDINGS_DIR, filename)

@app.route('/snapshots')
@requires_auth
def snapshots_gallery():
    files = sorted([f for f in os.listdir(SNAPSHOTS_DIR) if f.endswith('.jpg')], reverse=True)
    if not files: return "<h1>Snapshots</h1><p>No snapshots yet.</p><a href='/'>Back</a>"
    html = "<h1>Motion Snapshots</h1><nav><a href='/'>← Back</a></nav><br><div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:20px;'>"
    for f in files:
        p = url_for('serve_snapshot', filename=f)
        html += f"<div style='text-align:center;'><img src='{p}' style='width:100%;border-radius:8px;border:3px solid #555;'><br><small>{f}</small><br><a href='{p}' download>Download</a></div>"
    html += "</div>"
    return html

@app.route('/snapshots/<filename>')
@requires_auth
def serve_snapshot(filename):
    return send_from_directory(SNAPSHOTS_DIR, filename)

# === GUI ADMIN PANEL (unchanged) ===
@app.route('/admin')
@requires_auth
def admin_gui():
    return render_template_string("""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Admin Panel - SecureCam DVR</title>
        <style>
            body { background:#121212; color:#fff; font-family:Arial; padding:20px; }
            h1, h2, h3 { color:#0f0; }
            a { color:#00ff99; }
            table { width:100%; border-collapse:collapse; margin:20px 0; }
            th, td { border:1px solid #444; padding:10px; text-align:left; }
            input, select, button { padding:8px; margin:5px 0; background:#222; color:#fff; border:1px solid #444; }
            button { background:#0f0; color:#000; border:none; cursor:pointer; }
            button.delete { background:#f00; color:#fff; }
            form { margin:20px 0; }
        </style>
    </head>
    <body>
        <h1>Admin Panel</h1>
        <p><a href="/">← Back to Live View</a></p>

        <h2>General Settings</h2>
        <form action="/admin/save_general" method="post">
            Password: <input type="text" name="password" value="{{ password }}"><br>
            FPS: <input type="number" name="fps" value="{{ fps }}" min="5" max="60"><br>
            Grid Columns: <input type="number" name="camera_grid_columns" value="{{ cols }}" min="1" max="6"><br>
            Motion Sensitivity: <input type="number" name="motion_sensitivity" value="{{ sensitivity }}" min="1000" max="20000"><br>
            Snapshot Interval (sec): <input type="number" name="snapshot_min_interval" value="{{ snapshot_interval }}" min="1" max="60"><br>
            <button type="submit">Save Settings</button>
        </form>

        <h2>Cameras ({{ cameras|length }} found)</h2>
        <table>
            <tr><th>ID</th><th>Name</th><th>Type</th><th>Source / IP</th><th>Actions</th></tr>
            {% for cam in cameras %}
            <tr>
                <td>{{ loop.index }}</td>
                <td>
                    <form action="/admin/edit_camera/{{ loop.index0 }}" method="post" style="display:inline;">
                        <input type="text" name="name" value="{{ cam.name }}" style="width:200px;">
                        <button type="submit">Save</button>
                    </form>
                </td>
                <td>{{ cam.type|default('local') }}</td>
                <td>
                    {% if cam.type == 'securecam' %}{{ cam.ip }}:{{ cam.video_port|default('5554') }}
                    {% else %}{{ cam.source|default('') }}{% endif %}
                </td>
                <td>
                    <form action="/admin/delete_camera/{{ loop.index0 }}" method="post" style="display:inline;">
                        <button type="submit" class="delete">Delete</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
        </table>

        <h2>Add New Camera</h2>
        <form action="/admin/add_camera" method="post">
            Name: <input type="text" name="name" required style="width:200px;"><br><br>
            Type: 
            <select name="type">
                <option value="local">Local Webcam (USB/index)</option>
                <option value="rtsp">RTSP / Network Camera</option>
                <option value="securecam">SecureCam Client (LAN)</option>
            </select><br><br>
            Source / IP: <input type="text" name="source" placeholder="e.g. 0 or rtsp://... or 192.168.1.100" required style="width:300px;"><br><br>
            <button type="submit">Add Camera</button>
        </form>

        <p><small>SecureCam clients are auto-discovered via LAN scan. Manual add as fallback.</small></p>
    </body>
    </html>
    """,
    password=config['password'],
    fps=config['fps'],
    cols=config['camera_grid_columns'],
    sensitivity=config['motion_sensitivity'],
    snapshot_interval=config.get('snapshot_min_interval', 5),
    cameras=config['cameras'])

# Admin routes unchanged from previous version...
@app.route('/admin/save_general', methods=['POST'])
@requires_auth
def save_general():
    global config
    with config_lock:
        config['password'] = request.form['password']
        config['fps'] = int(request.form['fps'])
        config['camera_grid_columns'] = int(request.form['camera_grid_columns'])
        config['motion_sensitivity'] = int(request.form['motion_sensitivity'])
        config['snapshot_min_interval'] = int(request.form['snapshot_min_interval'])
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    restart_cameras()
    return redirect('/admin')

@app.route('/admin/add_camera', methods=['POST'])
@requires_auth
def add_camera():
    global config
    name = request.form['name']
    cam_type = request.form['type']
    source = request.form['source']

    new_cam = {"name": name}
    if cam_type == 'local':
        new_cam['source'] = int(source) if source.isdigit() else source
    elif cam_type == 'rtsp':
        new_cam['source'] = source
    elif cam_type == 'securecam':
        new_cam['type'] = 'securecam'
        new_cam['ip'] = source
        new_cam['video_port'] = config['securecam_video_port']

    with config_lock:
        config['cameras'].append(new_cam)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    restart_cameras()
    return redirect('/admin')

@app.route('/admin/edit_camera/<int:idx>', methods=['POST'])
@requires_auth
def edit_camera(idx):
    global config
    with config_lock:
        config['cameras'][idx]['name'] = request.form['name']
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    return redirect('/admin')

@app.route('/admin/delete_camera/<int:idx>', methods=['POST'])
@requires_auth
def delete_camera(idx):
    global config
    with config_lock:
        if 0 <= idx < len(config['cameras']):
            del config['cameras'][idx]
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
    restart_cameras()
    return redirect('/admin')

if __name__ == '__main__':
    print("\n=== SecureCam DVR Started ===")
    print("Fixed: config.json now loaded with UTF-8 encoding")
    print("GUI Admin + LAN auto-discovery for SecureCam")
    print("http://YOUR_IP:5000 | Login: admin / your_password")
    print("==========================================\n")
    app.run(host='0.0.0.0', port=5000, threaded=True)