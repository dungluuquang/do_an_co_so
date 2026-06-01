# app.py
import json
import os
import threading
import time
from datetime import date
from uuid import uuid4
from flask import Flask, render_template, request, abort, redirect, url_for, session, send_from_directory, g
from functools import wraps
import requests
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3
from dotenv import load_dotenv
from urllib.parse import quote

load_dotenv() 
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8899263192:AAHpTE2N-rX323dZ2deAI8yHmnHJuvp9uQA').strip()
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '5080522430').strip()
UTILITY_REMINDER_WINDOW_DAYS = int(os.getenv('UTILITY_REMINDER_WINDOW_DAYS', '2'))

DEFAULT_BANK_INFO = os.getenv('DEFAULT_BANK_INFO', '').strip()
DEFAULT_MERCHANT_NAME = os.getenv('DEFAULT_MERCHANT_NAME', 'CHU NHA TRO').strip()

app = Flask(__name__)
app.secret_key = 'demo-secret-key-2026'
app.jinja_env.globals['telegram_contact_link'] = lambda contact: format_telegram_contact_link(contact)

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
DATABASE = os.path.join(os.path.dirname(__file__), 'phongtro.db')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DEFAULT_ROOM_IMAGE = 'https://images.unsplash.com/photo-1493809842364-78817add7ffb?auto=format&fit=crop&w=800&q=80'

# ==========================================
# DATABASE CẤU HÌNH & MULTI-TENANT ISOLATION
# ==========================================
def ensure_db_schema():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        )
    ''')

    cur.execute("PRAGMA table_info('rooms')")
    cols = [r['name'] for r in cur.fetchall()]
    if 'status' not in cols:
        try: cur.execute("ALTER TABLE rooms ADD COLUMN status TEXT DEFAULT 'empty'")
        except: pass
    if 'owner_email' not in cols:
        try: cur.execute("ALTER TABLE rooms ADD COLUMN owner_email TEXT DEFAULT 'admin@demo.com'")
        except: pass

    cur.execute("PRAGMA table_info('utility_bills')")
    cols = [r['name'] for r in cur.fetchall()]
    if 'tenant_id' not in cols:
        try: cur.execute("ALTER TABLE utility_bills ADD COLUMN tenant_id INTEGER")
        except: pass

    cur.execute('''
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            id_number TEXT,
            contract_image TEXT,
            start_date TEXT,
            end_date TEXT,
            deposit INTEGER DEFAULT 0,
            room_id INTEGER,
            owner_email TEXT,
            created_at TEXT
        )
    ''')
    
    cur.execute("PRAGMA table_info('tenants')")
    cols = [r['name'] for r in cur.fetchall()]
    if 'owner_email' not in cols:
        try: cur.execute("ALTER TABLE tenants ADD COLUMN owner_email TEXT DEFAULT 'admin@demo.com'")
        except: pass

    db.commit()
    db.close()

ensure_db_schema()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def dict_from_row(row):
    if row is None: return None
    d = dict(row)
    if 'amenities' in d and d['amenities']:
        try: d['amenities'] = json.loads(d['amenities'])
        except: d['amenities'] = []
    if 'notifications' in d and d['notifications']:
        try: d['notifications'] = json.loads(d['notifications'])
        except: d['notifications'] = []
    return d

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def format_currency(amount):
    return f"{amount:,.0f}".replace(",", ".") + " ₫"

def mask_telegram_token(token):
    if not token: return 'Chưa cấu hình'
    if len(token) <= 12: return '*' * len(token)
    return token[:6] + '...' + token[-4:]

def format_telegram_contact_link(contact):
    cleaned = str(contact or '').strip()
    if not cleaned: return '#'
    if cleaned.startswith('https://t.me/') or cleaned.startswith('tg://'): return cleaned
    if cleaned.startswith('@'): return f"https://t.me/{cleaned[1:]}"
    digits = ''.join(char for char in cleaned if char.isdigit())
    if digits:
        if digits.startswith('0') and len(digits) >= 10: return f"https://t.me/+84{digits[1:]}"
        if digits.startswith('84') and len(digits) >= 11: return f"https://t.me/+{digits}"
        return f"https://t.me/+{digits}"
    return f"https://t.me/{cleaned}"

def get_telegram_bot_token(): return TELEGRAM_BOT_TOKEN
def get_telegram_chat_id(): return TELEGRAM_CHAT_ID

def build_monthly_statistics(bills):
    grouped = {}
    for bill in bills:
        key = (bill['year'], bill['month'])
        if key not in grouped:
            grouped[key] = {
                'year': bill['year'], 'month': bill['month'],
                'total_bills': 0, 'total_amount': 0, 'paid_count': 0,
                'pending_count': 0, 'overdue_count': 0,
                'total_electricity_usage': 0, 'total_water_usage': 0,
            }
        record = grouped[key]
        record['total_bills'] += 1
        record['total_amount'] += bill['amount']
        record['total_electricity_usage'] += bill.get('electricity_usage', bill['electricity_end'] - bill['electricity_start'])
        record['total_water_usage'] += bill.get('water_usage', bill['water_end'] - bill['water_start'])

        if bill['status'] == 'paid':
            record['paid_count'] += 1
        else:
            record['pending_count'] += 1
            if bill['due_date'] < date.today().isoformat():
                record['overdue_count'] += 1
    return [grouped[key] for key in sorted(grouped, reverse=True)]

def build_utility_notification_message(bill, room, custom_amount=None):
    due_date = bill['due_date']
    electricity_usage = bill.get('electricity_usage', bill['electricity_end'] - bill['electricity_start'])
    water_usage = bill.get('water_usage', bill['water_end'] - bill['water_start'])
    warning = '⚠️ Quá hạn thanh toán' if (date.today() > date.fromisoformat(due_date)) else '🔔 Nhắc nhở thanh toán tiện ích'
    
    display_amount = format_currency(bill['amount'])
    if custom_amount:
        try:
            display_amount = format_currency(int(custom_amount))
        except Exception:
            pass

    return (
        f"{warning}\n"
        f"Phòng: {room['title']}\n"
        f"Kỳ: {bill['month']}/{bill['year']}\n"
        f"Điện: {bill['electricity_start']} -> {bill['electricity_end']} (tiêu thụ {electricity_usage} kWh)\n"
        f"Nước: {bill['water_start']} -> {bill['water_end']} (tiêu thụ {water_usage} m³)\n"
        f"Tổng tiền: {display_amount}\n"
        f"Hạn thanh toán: {due_date}\n"
        f"Vui lòng thanh toán sớm để tránh phát sinh phí."
    )

def send_telegram_notification(bill, account_number=None, account_name=None, custom_note=None, custom_amount=None):
    room = get_room(bill['room_id'])
    if room is None: return {'success': False, 'error': 'Không tìm thấy phòng liên quan.'}
    
    bot_token = get_telegram_bot_token()
    chat_id = get_telegram_chat_id()
    if not bot_token or not chat_id: return {'success': False, 'error': 'Chưa cấu hình TELEGRAM_BOT_TOKEN hoặc CHAT_ID.'}

    # Xử lý số tiền
    qr_amount_str = str(bill['amount'])
    if custom_amount:
        try:
            clean_amt = ''.join(filter(str.isdigit, custom_amount))
            if clean_amt: qr_amount_str = clean_amt
        except Exception: pass

    message_text = build_utility_notification_message(bill, room, qr_amount_str)
    qr_image_bytes = None
    
    acc_info = account_number if account_number else DEFAULT_BANK_INFO
    merchant_name = account_name if account_name else DEFAULT_MERCHANT_NAME
    note = custom_note if custom_note else f"Phong {room['id']} T{bill['month']}_{bill['year']}"
    
    if acc_info and '-' in acc_info:
        bank_code, acc_num = acc_info.split('-', 1)
        qr_api_url = f"https://img.vietqr.io/image/{bank_code.strip()}-{acc_num.strip()}-compact2.jpg?amount={qr_amount_str}&addInfo={quote(note)}&accountName={quote(merchant_name)}"
        
        try:
            res = requests.get(qr_api_url, timeout=10)
            if res.status_code == 200: qr_image_bytes = res.content
        except Exception: pass

    try:
        if qr_image_bytes:
            files = {'photo': ('qr_payment.jpg', qr_image_bytes, 'image/jpeg')}
            params = {'chat_id': chat_id, 'caption': message_text}
            response = requests.post(f"https://api.telegram.org/bot{bot_token}/sendPhoto", files=files, data=params, timeout=15)
        else:
            payload = {'chat_id': chat_id, 'text': message_text, 'disable_web_page_preview': True}
            response = requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=payload, timeout=15)
        
        response.raise_for_status()
        return {'success': True, 'error': None, 'provider': 'telegram', 'status_code': response.status_code}
    except requests.RequestException as exc:
        return {'success': False, 'error': str(exc)}

def check_pending_utility_notifications():
    db = get_db()
    bills_rows = db.execute("SELECT * FROM utility_bills WHERE status != 'paid'").fetchall()
    bills = [dict_from_row(r) for r in bills_rows]
    today = date.today()
    updated = False

    for bill in bills:
        due_date = date.fromisoformat(bill['due_date'])
        days_left = (due_date - today).days
        if bill.get('telegram_status') == 'sent': continue

        if days_left <= UTILITY_REMINDER_WINDOW_DAYS or days_left < 0:
            result = send_telegram_notification(bill)
            notification_time = today.isoformat()
            t_status = 'sent' if result['success'] else 'failed'
            
            notifications = bill.get('notifications', [])
            notifications.append({
                'sent_at': notification_time, 'success': result['success'],
                'error': result.get('error'), 'provider': result.get('provider'),
                'status_code': result.get('status_code')
            })

            db.execute('''
                UPDATE utility_bills 
                SET telegram_status=?, last_notification_at=?, last_notification_result=?, 
                    last_notification_error=?, last_notification_status_code=?, 
                    last_notification_response=?, notifications=?
                WHERE id=?
            ''', (
                t_status, notification_time, result['success'], result.get('error'),
                result.get('status_code'), None, json.dumps(notifications), bill['id']
            ))
            updated = True

    if updated: db.commit()

worker_started = False
def start_utility_notification_worker():
    global worker_started
    if worker_started: return
    worker_started = True

    def worker():
        while True:
            with app.app_context():
                try: check_pending_utility_notifications()
                except Exception: pass
            time.sleep(60)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get('user_email'):
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped_view

def get_room(room_id):
    db = get_db()
    row = db.execute('SELECT * FROM rooms WHERE id = ?', (room_id,)).fetchone()
    return dict_from_row(row)

# ==========================================
# CONTROLLERS / ROUTES
# ==========================================

@app.route('/', methods=['GET'])
def index():
    search_query = request.args.get('q', '').lower().strip()
    max_price_query = request.args.get('max_price', type=int)
    
    db = get_db()
    query = "SELECT * FROM rooms WHERE status = 'empty'"
    params = []

    if search_query:
        query += ' AND (LOWER(title) LIKE ? OR LOWER(location) LIKE ?)'
        params.extend([f'%{search_query}%', f'%{search_query}%'])
        
    if max_price_query:
        query += ' AND price <= ?'
        params.append(max_price_query)

    rows = db.execute(query, params).fetchall()
    filtered_rooms = [dict_from_row(row) for row in rows]

    return render_template('index.html', rooms=filtered_rooms, current_query=search_query, current_price=max_price_query)

@app.route('/room/<int:room_id>', methods=['GET'])
def room_detail(room_id):
    room = get_room(room_id)
    if room is None: abort(404)
    return render_template('detail.html', room=room)


@app.route('/register', methods=['GET', 'POST'])
def register():
    message = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()

        if not email or not password:
            message = 'Email và mật khẩu không được để trống.'
        elif password != confirm_password:
            message = 'Mật khẩu xác nhận không khớp.'
        else:
            db = get_db()
            existing_user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if existing_user:
                message = 'Email này đã được đăng ký! Vui lòng chọn email khác.'
            else:
                hashed_password = generate_password_hash(password)
                db.execute('INSERT INTO users (email, password_hash) VALUES (?, ?)', (email, hashed_password))
                db.commit()
                return redirect(url_for('login', success='Đăng ký thành công! Vui lòng đăng nhập.'))
    
    return render_template('register.html', message=message)

@app.route('/login', methods=['GET', 'POST'])
def login():
    message = request.args.get('error')
    success_message = request.args.get('success')
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not email or not password:
            message = 'Email và mật khẩu không được để trống.'
        else:
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
            if user and check_password_hash(user['password_hash'], password):
                session['user_email'] = email
                next_url = request.args.get('next') or url_for('manage_rooms')
                return redirect(next_url)
            else:
                message = 'Email hoặc mật khẩu không chính xác.'
    return render_template('login.html', message=message, success_message=success_message)

@app.route('/logout', methods=['GET'])
def logout():
    session.pop('user_email', None)
    return redirect(url_for('index'))

@app.route('/post', methods=['GET', 'POST'])
@login_required
def post_room():
    status = None
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        price = request.form.get('price', type=int)
        area = request.form.get('area', type=int)
        location = request.form.get('location', '').strip()
        description = request.form.get('description', '').strip()
        contact = request.form.get('contact', '').strip()
        amenities = [a.strip() for a in request.form.get('amenities', '').split(',') if a.strip()]
        image_file = request.files.get('image_file')

        if not title or not price or not area or not location or not contact:
            status = 'Vui lòng điền đầy đủ tiêu đề, giá, diện tích, địa điểm và số liên hệ.'
        else:
            image_value = DEFAULT_ROOM_IMAGE
            if image_file and allowed_file(image_file.filename):
                filename = f"{uuid4().hex}_{secure_filename(image_file.filename)}"
                image_file.save(os.path.join(UPLOAD_FOLDER, filename))
                image_value = url_for('uploaded_file', filename=filename)

            db = get_db()
            db.execute('''
                INSERT INTO rooms (title, price, area, location, description, image, amenities, contact, owner_email)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (title, price, area, location, description, image_value, json.dumps(amenities), contact, session['user_email']))
            db.commit()
            return redirect(url_for('manage_rooms'))
    return render_template('post.html', status=status)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/manage', methods=['GET'])
@login_required
def manage_rooms():
    db = get_db()
    current_user = session['user_email']
    
    rooms = [dict_from_row(r) for r in db.execute('SELECT * FROM rooms WHERE owner_email = ?', (current_user,)).fetchall()]
    tenants = [dict_from_row(t) for t in db.execute('SELECT * FROM tenants WHERE owner_email = ?', (current_user,)).fetchall()]
    
    bills_query = '''
        SELECT b.* FROM utility_bills b
        JOIN rooms r ON b.room_id = r.id
        WHERE r.owner_email = ?
    '''
    bills = [dict_from_row(b) for b in db.execute(bills_query, (current_user,)).fetchall()]

    total_rooms = len(rooms)
    rented_rooms = sum(1 for r in rooms if r.get('status') == 'rented')
    empty_rooms = sum(1 for r in rooms if r.get('status') == 'empty')
    repair_rooms = sum(1 for r in rooms if r.get('status') == 'repair')

    occupancy_rate = (rented_rooms / total_rooms * 100) if total_rooms else 0
    expected_monthly_rent = sum(r.get('price', 0) for r in rooms if r.get('status') == 'rented')
    today = date.today()
    actual_collected = sum(b['amount'] for b in bills if b['status'] == 'paid' and b.get('year') == today.year and b.get('month') == today.month)

    expiring = []
    for t in tenants:
        if t.get('end_date'):
            try:
                days_left = (date.fromisoformat(t.get('end_date')) - today).days
                if 0 <= days_left <= 30:
                    expiring.append({'tenant': t, 'days_left': days_left})
            except: pass

    return render_template('manage.html', rooms=rooms, tenants=tenants, bills=bills,
                           total_rooms=total_rooms, rented_rooms=rented_rooms, empty_rooms=empty_rooms,
                           repair_rooms=repair_rooms, occupancy_rate=occupancy_rate,
                           expected_monthly_rent=expected_monthly_rent, actual_collected=actual_collected,
                           expiring=expiring)

@app.route('/manage/utility-bills', methods=['GET', 'POST'])
@login_required
def manage_utility_bills():
    db = get_db()
    current_user = session['user_email']
    status_message = request.args.get('status_message')
    error_message = request.args.get('error_message')

    if request.method == 'POST':
        room_id = request.form.get('room_id', type=int)
        tenant_id = request.form.get('tenant_id', type=int)
        month = request.form.get('month', type=int) or date.today().month
        year = request.form.get('year', type=int) or date.today().year
        electricity_start = request.form.get('electricity_start', type=int)
        electricity_end = request.form.get('electricity_end', type=int)
        water_start = request.form.get('water_start', type=int)
        water_end = request.form.get('water_end', type=int)
        
        electricity_price = request.form.get('electricity_price', type=int)
        water_price = request.form.get('water_price', type=int)
        due_date = request.form.get('due_date', '').strip()

        if not room_id or not due_date or electricity_price is None or water_price is None:
            error_message = 'Vui lòng chọn phòng, nhập đơn giá và hạn thanh toán.'
        else:
            try: due_date_obj = date.fromisoformat(due_date)
            except ValueError: due_date_obj = None

            room = get_room(room_id)
            if room is None or room.get('owner_email') != current_user:
                error_message = 'Phòng không tồn tại hoặc bạn không có quyền.'
            elif due_date_obj and (electricity_end is None or electricity_start is None or water_end is None or water_start is None):
                error_message = 'Vui lòng nhập đầy đủ chỉ số điện và nước.'
            elif due_date_obj and electricity_end < electricity_start:
                error_message = 'Chỉ số điện mới phải lớn hơn hoặc bằng chỉ số cũ.'
            elif due_date_obj and water_end < water_start:
                error_message = 'Chỉ số nước mới phải lớn hơn hoặc bằng chỉ số cũ.'
            else:
                electricity_usage = electricity_end - electricity_start
                water_usage = water_end - water_start
                amount = (electricity_usage * electricity_price) + (water_usage * water_price)

                db.execute('''
                    INSERT INTO utility_bills (
                        room_id, tenant_id, month, year, electricity_start, electricity_end, water_start, water_end, 
                        electricity_usage, water_usage, amount, due_date, status, telegram_status, created_at, notifications
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    room_id, tenant_id, month, year, electricity_start, electricity_end, water_start, water_end,
                    electricity_usage, water_usage, amount, due_date_obj.isoformat(), 'pending', 'not_sent', date.today().isoformat(), '[]'
                ))
                db.commit()
                status_message = 'Tạo hóa đơn tiện ích thành công.'

    search_query = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    month_filter = request.args.get('month', type=int)
    year_filter = request.args.get('year', type=int)

    query = '''
        SELECT b.*, r.title as room_title, t.name as tenant_name
        FROM utility_bills b 
        JOIN rooms r ON b.room_id = r.id 
        LEFT JOIN tenants t ON b.tenant_id = t.id
        WHERE r.owner_email = ?
    '''
    params = [current_user]

    if search_query:
        query += ' AND LOWER(r.title) LIKE ?'
        params.append(f'%{search_query.lower()}%')
    if status_filter:
        if status_filter == 'overdue':
            query += ' AND b.status != "paid" AND b.due_date < ?'
            params.append(date.today().isoformat())
        elif status_filter == 'pending':
            query += ' AND b.status != "paid" AND b.due_date >= ?'
            params.append(date.today().isoformat())
        elif status_filter == 'paid':
            query += ' AND b.status = "paid"'
    if month_filter:
        query += ' AND b.month = ?'
        params.append(month_filter)
    if year_filter:
        query += ' AND b.year = ?'
        params.append(year_filter)

    query += ' ORDER BY b.year DESC, b.month DESC, b.id DESC'

    bills = [dict_from_row(row) for row in db.execute(query, params).fetchall()]
    rooms = [dict_from_row(r) for r in db.execute('SELECT * FROM rooms WHERE owner_email = ?', (current_user,)).fetchall()]
    tenants = [dict_from_row(t) for t in db.execute('SELECT * FROM tenants WHERE owner_email = ?', (current_user,)).fetchall()]

    monthly_statistics = build_monthly_statistics(bills)
    total_amount = sum(b['amount'] for b in bills)
    total_electricity_usage = sum(b.get('electricity_usage', b['electricity_end'] - b['electricity_start']) for b in bills)
    total_water_usage = sum(b.get('water_usage', b['water_end'] - b['water_start']) for b in bills)
    paid_count = sum(1 for b in bills if b['status'] == 'paid')
    pending_count = sum(1 for b in bills if b['status'] != 'paid')
    overdue_count = sum(1 for b in bills if b['status'] != 'paid' and b['due_date'] < date.today().isoformat())

    # Xử lý bóc tách thông tin mặc định cho giao diện
    def_bank = 'MB'
    def_acc = ''
    if DEFAULT_BANK_INFO and '-' in DEFAULT_BANK_INFO:
        def_bank, def_acc = DEFAULT_BANK_INFO.split('-', 1)
    elif DEFAULT_BANK_INFO:
        def_acc = DEFAULT_BANK_INFO

    return render_template(
        'utility_bills.html', rooms=rooms, bills=bills, tenants=tenants,
        status_message=status_message, error_message=error_message,
        has_telegram_config=bool(get_telegram_bot_token() and get_telegram_chat_id()),
        telegram_token_masked=mask_telegram_token(get_telegram_bot_token()),
        telegram_chat_id=get_telegram_chat_id(), total_amount=total_amount,
        total_electricity_usage=total_electricity_usage, total_water_usage=total_water_usage,
        paid_count=paid_count, pending_count=pending_count, overdue_count=overdue_count,
        monthly_statistics=monthly_statistics, now=date.today(),
        search_query=search_query, status_filter=status_filter, month_filter=month_filter, year_filter=year_filter,
        def_bank=def_bank.strip(), def_acc=def_acc.strip(), default_merchant_name=DEFAULT_MERCHANT_NAME
    )

@app.route('/manage/tenants', methods=['GET', 'POST'])
@login_required
def manage_tenants():
    db = get_db()
    current_user = session['user_email']
    status = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone = request.form.get('phone', '').strip()
        id_number = request.form.get('id_number', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        deposit = request.form.get('deposit', type=int) or 0
        room_id = request.form.get('room_id', type=int)
        contract_file = request.files.get('contract_file')

        if not name or not room_id:
            status = 'Vui lòng nhập tên và chọn phòng.'
        else:
            room = get_room(room_id)
            if room and room.get('owner_email') == current_user:
                contract_path = ''
                if contract_file and allowed_file(contract_file.filename):
                    filename = f"{uuid4().hex}_{secure_filename(contract_file.filename)}"
                    contract_file.save(os.path.join(UPLOAD_FOLDER, filename))
                    contract_path = url_for('uploaded_file', filename=filename)

                db.execute('''
                    INSERT INTO tenants (name, phone, id_number, contract_image, start_date, end_date, deposit, room_id, owner_email, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, phone, id_number, contract_path, start_date or None, end_date or None, deposit, room_id, current_user, date.today().isoformat()))
                db.execute("UPDATE rooms SET status = 'rented' WHERE id = ?", (room_id,))
                db.commit()
                status = 'Thêm người thuê thành công.'

    rooms = [dict_from_row(r) for r in db.execute('SELECT * FROM rooms WHERE owner_email = ?', (current_user,)).fetchall()]
    tenants = [dict_from_row(t) for t in db.execute('SELECT * FROM tenants WHERE owner_email = ? ORDER BY id DESC', (current_user,)).fetchall()]
    return render_template('tenants.html', tenants=tenants, rooms=rooms, status=status)

@app.route('/manage/tenants/delete/<int:tenant_id>', methods=['POST'])
@login_required
def delete_tenant(tenant_id):
    db = get_db()
    t = db.execute('SELECT * FROM tenants WHERE id = ? AND owner_email = ?', (tenant_id, session['user_email'])).fetchone()
    if t:
        try: db.execute("UPDATE rooms SET status = 'empty' WHERE id = ?", (t['room_id'],))
        except: pass
        db.execute('DELETE FROM tenants WHERE id = ?', (tenant_id,))
        db.commit()
    return redirect(url_for('manage_tenants'))

@app.route('/manage/utility-bills/<int:bill_id>/send', methods=['POST'])
@login_required
def send_utility_bill_notification(bill_id):
    db = get_db()
    row = db.execute('''
        SELECT b.* FROM utility_bills b 
        JOIN rooms r ON b.room_id = r.id 
        WHERE b.id = ? AND r.owner_email = ?
    ''', (bill_id, session['user_email'])).fetchone()
    if row is None: abort(403)
    
    bill = dict_from_row(row)
    
    bank_code = request.form.get('bank_code', '').strip()
    account_number = request.form.get('account_number', '').strip()
    account_name = request.form.get('account_name', '').strip()
    custom_amount = request.form.get('custom_amount', '').strip()
    custom_note = request.form.get('custom_note', '').strip()

    full_account = f"{bank_code}-{account_number}" if account_number else None

    result = send_telegram_notification(bill, full_account, account_name, custom_note, custom_amount)
    
    t_status = 'sent' if result['success'] else 'failed'
    notifications = bill.get('notifications', [])
    notifications.append({
        'sent_at': date.today().isoformat(), 'success': result['success'],
        'error': result.get('error'), 'provider': result.get('provider'),
        'status_code': result.get('status_code')
    })

    db.execute('''
        UPDATE utility_bills 
        SET telegram_status=?, last_notification_at=?, last_notification_result=?, 
            last_notification_error=?, last_notification_status_code=?, 
            last_notification_response=?, notifications=?
        WHERE id=?
    ''', (
        t_status, date.today().isoformat(), result['success'], result.get('error'),
        result.get('status_code'), None, json.dumps(notifications), bill_id
    ))
    db.commit()
    return redirect(url_for('manage_utility_bills'))

@app.route('/manage/utility-bills/<int:bill_id>/mark-paid', methods=['POST'])
@login_required
def mark_utility_bill_paid(bill_id):
    db = get_db()
    row = db.execute('''
        SELECT b.id FROM utility_bills b 
        JOIN rooms r ON b.room_id = r.id 
        WHERE b.id = ? AND r.owner_email = ?
    ''', (bill_id, session['user_email'])).fetchone()
    
    if row:
        db.execute("UPDATE utility_bills SET status = 'paid' WHERE id = ?", (bill_id,))
        db.commit()
    return redirect(url_for('manage_utility_bills'))

@app.route('/manage/edit/<int:room_id>', methods=['GET', 'POST'])
@login_required
def edit_room(room_id):
    room = get_room(room_id)
    if room is None or room.get('owner_email') != session['user_email']: abort(403)

    status = None
    if request.method == 'POST':
        title = request.form.get('title', room['title']).strip()
        price = request.form.get('price', type=int) or room['price']
        area = request.form.get('area', type=int) or room['area']
        location = request.form.get('location', room['location']).strip()
        description = request.form.get('description', room['description']).strip()
        contact = request.form.get('contact', room['contact']).strip()
        amenities = [a.strip() for a in request.form.get('amenities', ', '.join(room['amenities'])).split(',') if a.strip()]
        
        db = get_db()
        db.execute('''
            UPDATE rooms SET title=?, price=?, area=?, location=?, description=?, contact=?, amenities=?
            WHERE id=?
        ''', (title, price, area, location, description, contact, json.dumps(amenities), room_id))
        db.commit()
        status = 'Thông tin phòng đã được cập nhật thành công.'
        room = get_room(room_id)

    return render_template('edit.html', room=room, status=status)

@app.route('/manage/delete/<int:room_id>', methods=['POST'])
@login_required
def delete_room(room_id):
    db = get_db()
    db.execute('DELETE FROM rooms WHERE id = ? AND owner_email = ?', (room_id, session['user_email']))
    db.commit()
    return redirect(url_for('manage_rooms'))

@app.route('/request-callback/<int:room_id>', methods=['POST'])
def request_callback(room_id):
    room = get_room(room_id)
    if room is None: abort(404)
        
    guest_name = request.form.get('guest_name', '').strip()
    guest_phone = request.form.get('guest_phone', '').strip()
    
    if not guest_name or not guest_phone:
        return redirect(url_for('room_detail', room_id=room_id, error='missing_data'))
        
    bot_token = get_telegram_bot_token()
    chat_id = get_telegram_chat_id()
    
    if bot_token and chat_id:
        message = (
            f"📞 <b>CÓ YÊU CẦU GỌI LẠI TỪ KHÁCH HÀNG!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f" <b>Phòng quan tâm:</b> {room['title']} (ID: {room['id']})\n"
            f" <b>Khu vực:</b> {room['location']}\n"
            f" <b>Tên khách hàng:</b> {guest_name}\n"
            f" <b>Số điện thoại:</b> <code>{guest_phone}</code>\n"
            f"<i>(Bấm vào số điện thoại để gọi hoặc copy)</i>"
        )
        try: requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", data={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'}, timeout=10)
        except Exception: pass
            
    return redirect(url_for('room_detail', room_id=room_id, success='1'))

if __name__ == '__main__':
    start_utility_notification_worker()
    app.run(debug=True, host='127.0.0.1', port=5000)
