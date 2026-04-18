"""
SafeKeep POS — Flask REST API Backend
Goods Storage & Handling System

Setup:
    pip install flask flask-cors pillow qrcode python-barcode apscheduler

Run:
    python app.py

Production:
    gunicorn -w 4 -b 0.0.0.0:5000 app:app
"""

import os
import uuid
import string
import random
import base64
import json
import io
import logging
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

# Optional libraries (graceful degradation)
try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

try:
    import barcode
    from barcode.writer import ImageWriter
    HAS_BARCODE = True
except ImportError:
    HAS_BARCODE = False

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False

# ── DATABASE (SQLite via built-in sqlite3) ──────────────────────
import sqlite3

DB_PATH = os.environ.get('SK_DB_PATH', 'safekeep.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS staff (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            username    TEXT UNIQUE NOT NULL,
            password    TEXT NOT NULL,  -- In production: use bcrypt hash
            role        TEXT DEFAULT 'staff' CHECK(role IN ('admin','staff')),
            created_at  TEXT DEFAULT (datetime('now')),
            is_active   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS customers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            phone       TEXT NOT NULL,
            id_number   TEXT,
            photo_path  TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS storage_locations (
            id          TEXT PRIMARY KEY,
            zone        TEXT NOT NULL CHECK(zone IN ('A','B','C')),
            shelf       TEXT NOT NULL,
            capacity    TEXT DEFAULT 'medium' CHECK(capacity IN ('small','medium','large')),
            is_active   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS items (
            id              TEXT PRIMARY KEY,
            secret          TEXT NOT NULL,
            barcode_data    TEXT NOT NULL UNIQUE,
            customer_id     TEXT REFERENCES customers(id),
            item_type       TEXT NOT NULL,
            item_qty        INTEGER DEFAULT 1,
            item_color      TEXT,
            item_value      REAL,
            item_notes      TEXT,
            storage_location TEXT REFERENCES storage_locations(id),
            duration_hours  INTEGER NOT NULL DEFAULT 8,
            checkin_time    TEXT NOT NULL,
            expiry_time     TEXT NOT NULL,
            collect_time    TEXT,
            status          TEXT DEFAULT 'active' CHECK(status IN ('active','collected','deleted')),
            staff_id        INTEGER REFERENCES staff(id),
            delete_scheduled_at TEXT,
            fee             REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id     TEXT REFERENCES items(id),
            action      TEXT NOT NULL,
            performed_by INTEGER REFERENCES staff(id),
            performed_at TEXT DEFAULT (datetime('now')),
            details     TEXT,
            ip_address  TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Seed default settings
        INSERT OR IGNORE INTO settings VALUES ('biz_name', 'SafeKeep POS');
        INSERT OR IGNORE INTO settings VALUES ('delete_after_hours', '24');
        INSERT OR IGNORE INTO settings VALUES ('fee_per_hour', '50');
        INSERT OR IGNORE INTO settings VALUES ('overdue_threshold_hours', '2');
        INSERT OR IGNORE INTO settings VALUES ('sms_enabled', '0');

        -- Seed default admin
        INSERT OR IGNORE INTO staff (name, username, password, role)
        VALUES ('Admin User', 'admin', 'admin123', 'admin');

        -- Seed storage locations
        """)

        # Seed storage locations
        zones = {'A': 'small', 'B': 'medium', 'C': 'large'}
        for zone, capacity in zones.items():
            for shelf_num in range(1, 13):
                loc_id = f"{zone}{shelf_num}"
                conn.execute(
                    "INSERT OR IGNORE INTO storage_locations (id, zone, shelf, capacity) VALUES (?,?,?,?)",
                    (loc_id, zone, loc_id, capacity)
                )
        conn.commit()

# ── FLASK APP ───────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SK_SECRET_KEY', 'safekeep-dev-key-change-in-production')
CORS(app, resources={r"/api/*": {"origins": "*"}})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('safekeep')

PHOTO_DIR = os.path.join(os.path.dirname(__file__), 'photos')
os.makedirs(PHOTO_DIR, exist_ok=True)

# ── AUTH MIDDLEWARE ─────────────────────────────────────────────
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'error': 'Authentication required'}), 401
        with get_db() as conn:
            staff = conn.execute(
                "SELECT * FROM staff WHERE id=? AND is_active=1", (token,)
            ).fetchone()
        if not staff:
            return jsonify({'error': 'Invalid or expired token'}), 401
        request.staff = dict(staff)
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.staff.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

# ── HELPERS ─────────────────────────────────────────────────────
def get_setting(conn, key, default=''):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row['value'] if row else default

def generate_receipt_id(conn):
    year = datetime.now().year
    last = conn.execute(
        "SELECT COUNT(*) as c FROM items WHERE checkin_time LIKE ?", (f"{year}%",)
    ).fetchone()['c']
    return f"GN-{year}-{str(last + 1).zfill(6)}"

def generate_secret(length=7):
    chars = string.ascii_uppercase.replace('I','').replace('O','') + string.digits[2:]
    return ''.join(random.SystemRandom().choice(chars) for _ in range(length))

def auto_assign_location(conn, preferred_zone=None):
    occupied = set(row['storage_location'] for row in conn.execute(
        "SELECT storage_location FROM items WHERE status='active' AND storage_location IS NOT NULL"
    ).fetchall())
    zones = [preferred_zone] if preferred_zone and preferred_zone in ('A','B','C') else ['A','B','C']
    for zone in zones:
        for num in range(1, 13):
            loc = f"{zone}{num}"
            if loc not in occupied:
                return loc
    return 'OVERFLOW'

def log_transaction(conn, item_id, action, staff_id, details=''):
    conn.execute(
        "INSERT INTO transactions (item_id, action, performed_by, details, ip_address) VALUES (?,?,?,?,?)",
        (item_id, action, staff_id, details, request.remote_addr)
    )

def save_photo(photo_b64, item_id):
    """Save base64 photo to disk and return path."""
    if not photo_b64:
        return None
    try:
        if ',' in photo_b64:
            photo_b64 = photo_b64.split(',')[1]
        data = base64.b64decode(photo_b64)
        path = os.path.join(PHOTO_DIR, f"{item_id}.jpg")
        with open(path, 'wb') as f:
            f.write(data)
        return path
    except Exception as e:
        logger.error(f"Photo save error: {e}")
        return None

def delete_photo(item_id):
    path = os.path.join(PHOTO_DIR, f"{item_id}.jpg")
    if os.path.exists(path):
        os.remove(path)

# ── AUTH ENDPOINTS ──────────────────────────────────────────────
@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    with get_db() as conn:
        staff = conn.execute(
            "SELECT * FROM staff WHERE username=? AND password=? AND is_active=1",
            (username, password)  # Production: use bcrypt.checkpw()
        ).fetchone()

    if not staff:
        return jsonify({'error': 'Invalid credentials'}), 401

    # Use staff.id as simple session token (production: use JWT)
    token = str(staff['id'])
    return jsonify({
        'token': token,
        'staff': {'id': staff['id'], 'name': staff['name'], 'role': staff['role']}
    })

@app.route('/api/auth/logout', methods=['POST'])
@require_auth
def logout():
    return jsonify({'message': 'Logged out'})

# ── CHECK-IN ENDPOINT ───────────────────────────────────────────
@app.route('/api/items/checkin', methods=['POST'])
@require_auth
def checkin():
    data = request.get_json()

    # Validate required fields
    required = ['customerName', 'customerPhone', 'itemType']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Missing required field: {field}'}), 400

    with get_db() as conn:
        receipt_id = generate_receipt_id(conn)
        secret = generate_secret()
        barcode_data = f"{receipt_id}|{secret}"

        preferred_zone = data.get('zone')
        location = data.get('storageLocation') or auto_assign_location(conn, preferred_zone)

        fee_per_hour = float(get_setting(conn, 'fee_per_hour', 50))
        duration_hours = int(data.get('durationHours', 8))
        fee = duration_hours * fee_per_hour

        now = datetime.now()
        expiry = now + timedelta(hours=duration_hours)
        delete_scheduled = expiry + timedelta(hours=float(get_setting(conn, 'delete_after_hours', 24)))

        # Save customer
        customer_id = str(uuid.uuid4())
        photo_path = save_photo(data.get('photo'), receipt_id)

        conn.execute("""
            INSERT INTO customers (id, name, phone, id_number, photo_path)
            VALUES (?,?,?,?,?)
        """, (customer_id, data['customerName'], data['customerPhone'],
               data.get('customerId'), photo_path))

        # Save item
        conn.execute("""
            INSERT INTO items (
                id, secret, barcode_data, customer_id, item_type, item_qty,
                item_color, item_value, item_notes, storage_location,
                duration_hours, checkin_time, expiry_time, status,
                staff_id, delete_scheduled_at, fee
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            receipt_id, secret, barcode_data, customer_id,
            data['itemType'], int(data.get('itemQty', 1)),
            data.get('itemColor'), data.get('itemValue'),
            data.get('itemNotes'), location,
            duration_hours, now.isoformat(), expiry.isoformat(),
            'active', request.staff['id'], delete_scheduled.isoformat(), fee
        ))

        log_transaction(conn, receipt_id, 'checkin', request.staff['id'],
                        f"Customer: {data['customerName']}, Location: {location}")
        conn.commit()

    logger.info(f"CHECK-IN: {receipt_id} for {data['customerName']} at {location}")
    return jsonify({
        'success': True,
        'receiptId': receipt_id,
        'secret': secret,
        'barcodeData': barcode_data,
        'storageLocation': location,
        'expiryTime': expiry.isoformat(),
        'fee': fee
    }), 201

# ── SCAN & RETRIEVE ─────────────────────────────────────────────
@app.route('/api/items/scan', methods=['POST'])
@require_auth
def scan():
    data = request.get_json()
    barcode_data = (data.get('barcodeData') or '').strip()

    if '|' not in barcode_data:
        return jsonify({'error': 'Invalid barcode format', 'valid': False}), 400

    receipt_id, secret = barcode_data.split('|', 1)

    with get_db() as conn:
        row = conn.execute("""
            SELECT i.*, c.name as customer_name, c.phone as customer_phone,
                   c.id_number, c.photo_path, s.name as staff_name
            FROM items i
            LEFT JOIN customers c ON i.customer_id = c.id
            LEFT JOIN staff s ON i.staff_id = s.id
            WHERE i.id = ?
        """, (receipt_id,)).fetchone()

        if not row:
            log_transaction(conn, receipt_id, 'scan_fail', request.staff['id'], 'Not found')
            conn.commit()
            return jsonify({'error': 'Receipt not found', 'valid': False}), 404

        item = dict(row)

        if item['secret'] != secret:
            log_transaction(conn, receipt_id, 'scan_fail', request.staff['id'], 'Secret mismatch — fraud alert')
            conn.commit()
            return jsonify({'error': 'Invalid secret code. Possible fraud.', 'valid': False}), 403

        if item['status'] == 'collected':
            return jsonify({
                'error': 'Item already collected',
                'valid': False,
                'collectTime': item['collect_time']
            }), 409

        # Load photo as base64
        photo_b64 = None
        if item.get('photo_path') and os.path.exists(item['photo_path']):
            with open(item['photo_path'], 'rb') as f:
                photo_b64 = 'data:image/jpeg;base64,' + base64.b64encode(f.read()).decode()

        log_transaction(conn, receipt_id, 'scan_ok', request.staff['id'], 'Valid scan')
        conn.commit()

    is_overdue = datetime.fromisoformat(item['expiry_time']) < datetime.now()

    return jsonify({
        'valid': True,
        'receiptId': item['id'],
        'customerName': item['customer_name'],
        'customerPhone': item['customer_phone'],
        'customerId': item['id_number'],
        'itemType': item['item_type'],
        'itemQty': item['item_qty'],
        'itemColor': item['item_color'],
        'itemNotes': item['item_notes'],
        'storageLocation': item['storage_location'],
        'checkinTime': item['checkin_time'],
        'expiryTime': item['expiry_time'],
        'fee': item['fee'],
        'isOverdue': is_overdue,
        'photo': photo_b64,
        'staffName': item['staff_name']
    })

# ── RELEASE ITEM ────────────────────────────────────────────────
@app.route('/api/items/<receipt_id>/release', methods=['POST'])
@require_auth
def release_item(receipt_id):
    with get_db() as conn:
        item = conn.execute("SELECT * FROM items WHERE id=?", (receipt_id,)).fetchone()
        if not item:
            return jsonify({'error': 'Item not found'}), 404
        if item['status'] != 'active':
            return jsonify({'error': 'Item not available for release'}), 409

        now = datetime.now()
        delete_after = float(get_setting(conn, 'delete_after_hours', 24))
        scheduled_delete = now + timedelta(hours=delete_after)

        conn.execute("""
            UPDATE items SET status='collected', collect_time=?, delete_scheduled_at=?
            WHERE id=?
        """, (now.isoformat(), scheduled_delete.isoformat(), receipt_id))

        log_transaction(conn, receipt_id, 'release', request.staff['id'],
                        f"Released by {request.staff['name']}")
        conn.commit()

    logger.info(f"RELEASE: {receipt_id} by staff {request.staff['name']}")
    return jsonify({'success': True, 'deleteScheduledAt': scheduled_delete.isoformat()})

# ── SEARCH ──────────────────────────────────────────────────────
@app.route('/api/items', methods=['GET'])
@require_auth
def list_items():
    q = request.args.get('q', '')
    status = request.args.get('status', '')
    zone = request.args.get('zone', '')
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    offset = (page - 1) * per_page

    conditions = ["i.status != 'deleted'"]
    params = []

    if q:
        conditions.append("(i.id LIKE ? OR c.name LIKE ? OR c.phone LIKE ? OR i.item_type LIKE ?)")
        params.extend([f'%{q}%'] * 4)

    if status == 'active':
        conditions.append("i.status = 'active'")
    elif status == 'collected':
        conditions.append("i.status = 'collected'")
    elif status == 'overdue':
        conditions.append("i.status = 'active' AND i.expiry_time < datetime('now')")

    if zone:
        conditions.append("i.storage_location LIKE ?")
        params.append(f"{zone}%")

    where = ' AND '.join(conditions)

    with get_db() as conn:
        total = conn.execute(f"""
            SELECT COUNT(*) as c FROM items i
            LEFT JOIN customers c ON i.customer_id = c.id
            WHERE {where}
        """, params).fetchone()['c']

        rows = conn.execute(f"""
            SELECT i.id, i.status, i.item_type, i.item_qty, i.storage_location,
                   i.checkin_time, i.expiry_time, i.fee,
                   c.name as customer_name, c.phone as customer_phone
            FROM items i
            LEFT JOIN customers c ON i.customer_id = c.id
            WHERE {where}
            ORDER BY i.checkin_time DESC
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

    return jsonify({
        'items': [dict(r) for r in rows],
        'total': total,
        'page': page,
        'pages': (total + per_page - 1) // per_page
    })

# ── STORAGE MAP ──────────────────────────────────────────────────
@app.route('/api/storage/map', methods=['GET'])
@require_auth
def storage_map():
    with get_db() as conn:
        occupied = {row['storage_location']: dict(row) for row in conn.execute("""
            SELECT i.storage_location, i.id, i.item_type, c.name as customer_name,
                   i.expiry_time, i.status
            FROM items i
            LEFT JOIN customers c ON i.customer_id = c.id
            WHERE i.status = 'active'
        """).fetchall()}

        locations = conn.execute("SELECT * FROM storage_locations ORDER BY zone, id").fetchall()

    result = {}
    for loc in locations:
        zone = loc['zone']
        if zone not in result:
            result[zone] = []
        slot_data = dict(loc)
        slot_data['occupied'] = loc['id'] in occupied
        if slot_data['occupied']:
            slot_data['item'] = occupied[loc['id']]
        result[zone].append(slot_data)

    return jsonify(result)

# ── QR CODE GENERATION ───────────────────────────────────────────
@app.route('/api/items/<receipt_id>/qr', methods=['GET'])
@require_auth
def generate_qr(receipt_id):
    with get_db() as conn:
        item = conn.execute("SELECT barcode_data FROM items WHERE id=?", (receipt_id,)).fetchone()
    if not item:
        return jsonify({'error': 'Not found'}), 404

    if HAS_QR:
        img = qrcode.make(item['barcode_data'])
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')
    else:
        return jsonify({'error': 'QR library not installed', 'data': item['barcode_data']}), 501

# ── DASHBOARD STATS ──────────────────────────────────────────────
@app.route('/api/stats', methods=['GET'])
@require_auth
def stats():
    today = datetime.now().date().isoformat()
    with get_db() as conn:
        active = conn.execute("SELECT COUNT(*) as c FROM items WHERE status='active'").fetchone()['c']
        collected = conn.execute("SELECT COUNT(*) as c FROM items WHERE status='collected'").fetchone()['c']
        today_count = conn.execute("SELECT COUNT(*) as c FROM items WHERE DATE(checkin_time)=?", (today,)).fetchone()['c']
        overdue = conn.execute("SELECT COUNT(*) as c FROM items WHERE status='active' AND expiry_time < datetime('now')").fetchone()['c']
        revenue = conn.execute("SELECT COALESCE(SUM(fee),0) as r FROM items").fetchone()['r']

    return jsonify({
        'active': active, 'collected': collected,
        'todayCheckins': today_count, 'overdue': overdue,
        'totalRevenue': revenue
    })

# ── SETTINGS ─────────────────────────────────────────────────────
@app.route('/api/settings', methods=['GET'])
@require_auth
def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return jsonify({r['key']: r['value'] for r in rows})

@app.route('/api/settings', methods=['PUT'])
@require_auth
@require_admin
def update_settings():
    data = request.get_json()
    with get_db() as conn:
        for key, value in data.items():
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(value)))
        conn.commit()
    return jsonify({'success': True})

# ── STAFF MANAGEMENT ─────────────────────────────────────────────
@app.route('/api/staff', methods=['GET'])
@require_auth
@require_admin
def list_staff():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, username, role, created_at, is_active FROM staff").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/staff', methods=['POST'])
@require_auth
@require_admin
def add_staff():
    data = request.get_json()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO staff (name, username, password, role) VALUES (?,?,?,?)",
                         (data['name'], data['username'], data['password'], data.get('role','staff')))
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({'error': 'Username already exists'}), 409
    return jsonify({'success': True}), 201

# ── CLEANUP JOB ──────────────────────────────────────────────────
def run_cleanup_job():
    """Background job: delete items past their scheduled deletion time."""
    try:
        with get_db() as conn:
            expired = conn.execute("""
                SELECT i.id FROM items i
                WHERE i.status = 'collected'
                AND i.delete_scheduled_at < datetime('now')
            """).fetchall()

            for row in expired:
                item_id = row['id']
                conn.execute("UPDATE items SET status='deleted' WHERE id=?", (item_id,))
                # Delete customer data
                conn.execute("""
                    UPDATE customers SET name='[DELETED]', phone='[DELETED]', id_number=NULL
                    WHERE id = (SELECT customer_id FROM items WHERE id=?)
                """, (item_id,))
                delete_photo(item_id)
                logger.info(f"AUTO-DELETED: {item_id}")

            if expired:
                conn.commit()
                logger.info(f"Cleanup: deleted {len(expired)} records")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# ── ACTIVITY LOGS ────────────────────────────────────────────────
@app.route('/api/logs', methods=['GET'])
@require_auth
def get_logs():
    limit = int(request.args.get('limit', 100))
    with get_db() as conn:
        rows = conn.execute("""
            SELECT t.*, s.name as staff_name, t.item_id
            FROM transactions t
            LEFT JOIN staff s ON t.performed_by = s.id
            ORDER BY t.performed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return jsonify([dict(r) for r in rows])

# ── HEALTH CHECK ─────────────────────────────────────────────────
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': '2.1.0', 'time': datetime.now().isoformat()})

# ── MAIN ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    logger.info("SafeKeep POS backend initialized")

    if HAS_SCHEDULER:
        scheduler = BackgroundScheduler()
        scheduler.add_job(run_cleanup_job, 'interval', hours=1, id='cleanup')
        scheduler.start()
        logger.info("Background cleanup scheduler started")
    else:
        logger.warning("APScheduler not installed — auto-cleanup disabled. Run manually via /api/admin/cleanup")

    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_ENV', 'development') == 'development'
    app.run(host='0.0.0.0', port=port, debug=debug)
