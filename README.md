# SafeKeep POS — Setup Guide
**Goods Storage & Handling System | v2.1.0**

---

## Overview

SafeKeep POS lets staff check in customer items (luggage, bags, parcels), generate secure QR/barcode receipts, store items in assigned locations, and verify/release them on return. All customer data auto-deletes after collection.

---

## Files Included

| File | Description |
|------|-------------|
| `pos-storage-system.html` | **Standalone frontend** — fully offline, works in any browser |
| `app.py` | Flask REST API backend |
| `schema.sql` | Full SQLite/PostgreSQL schema |
| `README.md` | This setup guide |

---

## Option A: Standalone HTML (Quickest — No Backend Needed)

The HTML file is fully self-contained with:
- IndexedDB / localStorage as the database
- QR code + barcode generation in-browser
- Webcam photo capture
- All 7 screens fully functional
- Works offline after first load

**Steps:**
1. Open `pos-storage-system.html` in any modern browser (Chrome recommended)
2. Login: `admin` / `admin123`
3. That's it — start checking items in immediately

**For printing receipts:** Use Ctrl+P / Cmd+P from the Receipt step

---

## Option B: Full Stack (Flask Backend + HTML Frontend)

### 1. Requirements

```bash
Python 3.9+
pip install flask flask-cors apscheduler qrcode pillow python-barcode
```

Optional for SMS (Africa's Talking):
```bash
pip install africastalking
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

Or manually:
```bash
pip install flask flask-cors apscheduler qrcode[pil] pillow python-barcode
```

### 3. Initialize the Database

```bash
python app.py
# Database is created automatically as safekeep.db on first run
```

Or run schema manually:
```bash
sqlite3 safekeep.db < schema.sql
```

### 4. Start the Server

```bash
python app.py
# Server starts at http://localhost:5000
```

For production:
```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

### 5. Connect Frontend to Backend

In `pos-storage-system.html`, update the API_BASE constant (if you add API integration):
```javascript
const API_BASE = 'http://localhost:5000/api';
```

---

## API Endpoints Reference

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/login` | Login — returns token |
| POST | `/api/auth/logout` | Logout |

**Login request:**
```json
{ "username": "admin", "password": "admin123" }
```

**Login response:**
```json
{ "token": "1", "staff": { "id": 1, "name": "Admin User", "role": "admin" } }
```

### Items
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/items/checkin` | Check in an item |
| POST | `/api/items/scan` | Scan & verify barcode |
| POST | `/api/items/{id}/release` | Release item to customer |
| GET | `/api/items` | List/search items |
| GET | `/api/items/{id}/qr` | Generate QR image |

**Check-in request:**
```json
{
  "customerName": "John Doe",
  "customerPhone": "+254 722 000 000",
  "customerId": "12345678",
  "itemType": "Luggage",
  "itemQty": 1,
  "itemColor": "Black",
  "itemNotes": "Fragile",
  "durationHours": 8,
  "zone": "B",
  "photo": "data:image/jpeg;base64,..."
}
```

**Scan request:**
```json
{ "barcodeData": "GN-2026-000001|X7K9P2A" }
```

### Storage & Stats
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/storage/map` | Full storage map |
| GET | `/api/stats` | Dashboard statistics |
| GET | `/api/logs` | Activity log |

### Admin
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/PUT | `/api/settings` | System settings |
| GET/POST | `/api/staff` | Staff management |

---

## Barcode / QR Format

Every receipt encodes:
```
GN-{YEAR}-{SEQUENCE}|{SECRET}

Example: GN-2026-000042|X7K9P2A
```

- **Receipt ID**: `GN-2026-000042` — unique, sequential
- **Secret**: `X7K9P2A` — random 7-char alphanumeric (case-insensitive lookalike chars excluded)
- **Separator**: `|` pipe character
- **Full barcode**: `GN-2026-000042|X7K9P2A`

On scan, the system validates:
1. ID exists in database
2. Secret matches exactly
3. Status is `active` (not already collected)

---

## Security Architecture

### Receipt Security
- Receipt ID is **sequential** — predictable but not guessable alone
- Secret code is **cryptographically random** — 7 chars from 30-char alphabet = 21.9 billion combinations
- Both required together — ID alone or secret alone is useless
- Collected receipts are **rejected on rescan**

### Authentication
- Session-based (token = staff ID) in the standalone HTML version
- JWT recommended for production (add python-jose or PyJWT)
- Admin role required for settings, staff management, force cleanup

### Data Privacy
- Customer data is automatically deleted after item collection + configured delay (default: 24 hours)
- Photos are stored locally and deleted with the record
- No data is sent to external servers in standalone mode

### For Production
- Hash passwords with bcrypt: `pip install bcrypt`
- Use HTTPS (SSL/TLS)
- Set `SK_SECRET_KEY` environment variable to a random string
- Use PostgreSQL instead of SQLite for multi-user setups

---

## Hardware Integration

### Barcode Scanner
Standard USB barcode scanners emulate keyboard input. The system:
1. Detects fast keyboard input (scanner is faster than human typing)
2. Reads the full barcode string
3. Auto-routes to the Scan & Retrieve page
4. Triggers verification automatically on Enter key

No driver or special setup needed — plug in scanner and it works.

### Receipt Printer
- Use Ctrl+P / Cmd+P from the Receipt step
- Configure printer as default in system settings
- For thermal printers (80mm), the receipt is sized to fit automatically
- Supports any printer with a driver installed

### Webcam
- Built-in browser webcam API (no drivers needed)
- Prompts for camera permission on first use
- Supports USB webcams, laptop cameras, and phone cameras (via browser)

### Optional: Bluetooth / Wi-Fi Scanner
- Bluetooth scanners that emulate keyboard work automatically
- Wi-Fi scanners: configure them to send HTTP POST to `/api/items/scan`

---

## Storage Zone Layout

```
ZONE A (Small items)    ZONE B (Medium items)    ZONE C (Large items)
┌──────────────────┐   ┌──────────────────┐     ┌──────────────────┐
│ A1  A2  A3  A4  │   │ B1  B2  B3  B4  │     │ C1  C2  C3  C4  │
│ A5  A6  A7  A8  │   │ B5  B6  B7  B8  │     │ C5  C6  C7  C8  │
│ A9  A10 A11 A12 │   │ B9  B10 B11 B12 │     │ C9  C10 C11 C12 │
└──────────────────┘   └──────────────────┘     └──────────────────┘
  12 slots                12 slots                 12 slots
  Total: 36 slots
```

Extend storage by adding more zones (D, E...) in the `ZONES` configuration.

---

## User Workflow

### Check-In (2–3 minutes)
1. Staff opens **New Check-In**
2. Enter customer name + phone number
3. Select item type, quantity, duration
4. Capture photo (webcam or upload)
5. System generates receipt with QR + barcode
6. Print receipt → hand to customer
7. Place item at displayed storage location (e.g. **B3**)

### Retrieval (30 seconds)
1. Customer returns with receipt
2. Staff scans barcode (or types ID)
3. System shows ✓ VERIFIED + customer photo
4. Staff visually confirms identity matches photo
5. Click **Release Item**
6. Retrieve item from displayed location
7. Data marked for deletion

### Overdue Items
- Items past their booked time show **OVERDUE** badge
- Staff can extend time or charge additional fee
- Auto-delete still occurs after collection

---

## Default Login Credentials

| Username | Password | Role |
|----------|----------|------|
| admin | admin123 | Administrator |
| staff | staff123 | Staff |

**⚠️ Change these immediately in production.**

---

## Scaling to PostgreSQL

Replace the SQLite connection with:
```python
import psycopg2
DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://user:pass@localhost/safekeep')
```

The schema is compatible with PostgreSQL with minor adjustments:
- Replace `INTEGER PRIMARY KEY AUTOINCREMENT` with `SERIAL PRIMARY KEY`
- Replace `datetime('now')` with `NOW()`
- Replace `TEXT` with `VARCHAR` or `TEXT` (both work in PostgreSQL)

---

## Troubleshooting

**Camera not working:** Allow camera permission in browser settings → chrome://settings/content/camera

**Barcode scanner not detected:** Ensure scanner is in USB HID (keyboard emulation) mode — check scanner manual

**Receipt won't print:** Try Ctrl+P and select your receipt printer; or use the browser's print dialog

**Data not saving:** Check browser's localStorage isn't blocked (private/incognito mode may limit storage)

**Backend won't start:** Run `pip install flask flask-cors` and check Python version (3.9+)

---

## License
MIT License — Free for commercial use
Built for real-world deployment at malls, bus stations, markets, and events.
