import os
import time
import base64
import numpy as np
from datetime import datetime, timezone, timedelta
from functools import wraps

# ── TIMEZONE WIB (UTC+7) ──
WIB = timezone(timedelta(hours=7))

def now_wib():
    """Return datetime sekarang dalam WIB (naive, untuk disimpan ke DB)."""
    return datetime.now(WIB).replace(tzinfo=None)

from flask import (Flask, render_template, request,
                   redirect, url_for, flash, jsonify, session)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin,
                         login_user, logout_user,
                         login_required, current_user)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import select, func, or_, text, inspect as sa_inspect
from PIL import Image
from authlib.integrations.flask_client import OAuth

# ── INIT ──
app      = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['SECRET_KEY']                     = os.environ.get('SECRET_KEY', 'ricescan-secret-2025-GANTI-INI')
# ── Support PostgreSQL di cloud (Render), fallback ke SQLite lokal ──
_db_url = os.environ.get('DATABASE_URL', f"sqlite:///{os.path.join(BASE_DIR, 'ricescan.db')}")
if _db_url.startswith('postgres://'):          # Render kadang pakai prefix lama
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MODEL']                          = None
MODEL_PATH = os.environ.get(
    'MODEL_PATH',
    os.path.join(BASE_DIR, '..', 'model', 'rice_leaf_model.keras')
)

# ── GOOGLE OAUTH CONFIG ──
app.config['GOOGLE_CLIENT_ID']     = os.environ.get('GOOGLE_CLIENT_ID', '')
app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET', '')

# ── AVATAR CONFIG ──
AVATAR_FOLDER   = os.path.join(BASE_DIR, 'static', 'uploads', 'avatars')
ALLOWED_EXT     = {'jpg', 'jpeg', 'png', 'webp'}
MAX_AVATAR_SIZE = 2 * 1024 * 1024  # 2 MB
os.makedirs(AVATAR_FOLDER, exist_ok=True)

db            = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view             = 'login'
login_manager.login_message          = 'Silakan login terlebih dahulu.'
login_manager.login_message_category = 'warning'

# ── OAUTH SETUP ──
oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=app.config['GOOGLE_CLIENT_ID'],
    client_secret=app.config['GOOGLE_CLIENT_SECRET'],
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

class_names          = ['blast', 'blight', 'tungro']
IMG_SIZE             = (299, 299)
MAX_FILE_SIZE        = 5 * 1024 * 1024
CONFIDENCE_THRESHOLD = 0.70
MARGIN_THRESHOLD     = 0.10

# ── MODELS ──
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer,     primary_key=True)
    nama          = db.Column(db.String(100), nullable=False)
    username      = db.Column(db.String(50),  unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)   # nullable → Google users tidak punya password
    role          = db.Column(db.String(20),  nullable=False, default='user')
    foto_profil   = db.Column(db.String(120), nullable=True, default=None)
    google_id     = db.Column(db.String(120), nullable=True, unique=True)
    google_avatar = db.Column(db.String(500), nullable=True)
    _is_active    = db.Column('is_active', db.Boolean, nullable=False, default=True)
    created_at    = db.Column(db.DateTime, default=now_wib)
    riwayat = db.relationship('RiwayatDeteksi', backref='user', lazy=True, cascade='all, delete-orphan')

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_active(self):
        return bool(self._is_active)

    @is_active.setter
    def is_active(self, value):
        self._is_active = value

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, pw)

    def avatar_url(self):
        if self.foto_profil:
            return url_for('static', filename=f'uploads/avatars/{self.foto_profil}')
        if self.google_avatar:
            return self.google_avatar
        return None


class RiwayatDeteksi(db.Model):
    __tablename__ = 'riwayat'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    hasil       = db.Column(db.String(100), nullable=False)
    emoji       = db.Column(db.String(10),  nullable=False)
    confidence  = db.Column(db.Float,       nullable=False)
    preview_b64 = db.Column(db.Text,        nullable=True)
    created_at  = db.Column(db.DateTime,    default=now_wib)


class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'
    id         = db.Column(db.Integer,  primary_key=True)
    user_id    = db.Column(db.Integer,  db.ForeignKey('users.id'), nullable=False)
    sender     = db.Column(db.String(10), nullable=False)   # 'user' or 'admin'
    message    = db.Column(db.Text,     nullable=False)
    created_at = db.Column(db.DateTime, default=now_wib)
    is_read    = db.Column(db.Boolean,  default=False)
    user       = db.relationship('User', backref=db.backref('chats', lazy=True))


class Notification(db.Model):
    """
    Notifikasi untuk user maupun admin.
    type: 'scan' | 'chat' | 'account' | 'new_user' | 'new_scan' | 'low_conf'
    """
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer,    primary_key=True)
    user_id    = db.Column(db.Integer,    db.ForeignKey('users.id'), nullable=False)
    type       = db.Column(db.String(30), nullable=False)
    title      = db.Column(db.String(200),nullable=False)
    message    = db.Column(db.Text,       nullable=False)
    link       = db.Column(db.String(300),nullable=True)
    is_read    = db.Column(db.Boolean,    default=False)
    created_at = db.Column(db.DateTime,   default=now_wib)
    user       = db.relationship('User',  backref=db.backref('notifications', lazy=True))


# ── HELPER: buat notifikasi, pertahankan maks 60 per user ──
def push_notif(user_id, type_, title, message, link=None):
    """Tambahkan notifikasi dan trim jika sudah > 60."""
    n = Notification(user_id=user_id, type=type_, title=title, message=message, link=link)
    db.session.add(n)
    # Trim: hapus yang paling lama jika > 60
    old = Notification.query.filter_by(user_id=user_id)\
          .order_by(Notification.created_at.desc()).offset(60).all()
    for o in old:
        db.session.delete(o)


def push_notif_all_admins(type_, title, message, link=None):
    """Kirim notifikasi ke semua admin yang aktif."""
    admins = db.session.execute(
        select(User).where(User.role == 'admin', User._is_active == True)
    ).scalars().all()
    for admin in admins:
        push_notif(admin.id, type_, title, message, link)


def time_ago(dt):
    """Return human-readable time ago string dalam bahasa Indonesia."""
    diff = now_wib() - dt
    s = int(diff.total_seconds())
    if s < 60:              return 'baru saja'
    if s < 3600:            return f'{s // 60} mnt lalu'
    if s < 86400:           return f'{s // 3600} jam lalu'
    if s < 86400 * 7:       return f'{s // 86400} hari lalu'
    return dt.strftime('%d %b %Y')


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ── DECORATORS ──
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Akses ditolak. Halaman ini hanya untuk admin.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated


# ── HELPERS ──
def get_model():
    if app.config['MODEL'] is None:
        from tensorflow.keras.models import load_model
        app.config['MODEL'] = load_model(MODEL_PATH)
    return app.config['MODEL']


disease_info = {
    'blast':  {'label':'Blast (Blas)',        'emoji':'🔴',
               'desc': 'Infeksi jamur Magnaporthe oryzae yang menyebabkan bercak berlian pada daun.',
               'saran':'Gunakan fungisida trifloxystrobin atau azoxystrobin. Hindari pemupukan nitrogen berlebih.'},
    'blight': {'label':'Blight (Hawar Daun)', 'emoji':'🟡',
               'desc': 'Infeksi bakteri Xanthomonas oryzae pv. oryzae, daun menguning dan mengering.',
               'saran':'Gunakan varietas tahan blight. Aplikasikan bakterisida berbasis tembaga.'},
    'tungro': {'label':'Tungro',              'emoji':'🔵',
               'desc': 'Penyakit virus ditularkan wereng hijau (Nephotettix virescens).',
               'saran':'Kendalikan wereng hijau dengan insektisida selektif. Tanam varietas tahan tungro.'},
}


def allowed_avatar(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def preprocess_image(file_storage):
    img = Image.open(file_storage).convert('RGB').resize(IMG_SIZE, Image.LANCZOS)
    return np.expand_dims(np.array(img, dtype=np.float32) / 255.0, axis=0)


def image_to_base64(file_storage):
    file_storage.seek(0)
    data = file_storage.read()
    mime = file_storage.mimetype or 'image/jpeg'
    return f'data:{mime};base64,{base64.b64encode(data).decode()}'


def count_rows(model, *conditions):
    stmt = select(func.count()).select_from(model)
    for c in conditions:
        stmt = stmt.where(c)
    return db.session.scalar(stmt) or 0


class Pagination:
    def __init__(self, page, per_page, total, items):
        self.page = page; self.per_page = per_page
        self.total = total; self.items = items
        self.pages = max(1, (total + per_page - 1) // per_page)
        self.has_prev = page > 1; self.has_next = page < self.pages
        self.prev_num = page - 1; self.next_num = page + 1

    def iter_pages(self, left_edge=1, right_edge=1, left_current=2, right_current=2):
        last = 0
        for n in range(1, self.pages + 1):
            if n <= left_edge or (self.page-left_current-1 < n < self.page+right_current) or n > self.pages-right_edge:
                if last + 1 != n: yield None
                yield n; last = n


def run_paginate(stmt, page, per_page):
    total = db.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = db.session.execute(stmt.offset((page-1)*per_page).limit(per_page)).scalars().all()
    return Pagination(page, per_page, total, items)


def save_avatar(file, user_id, old_filename=None):
    """Simpan foto avatar, hapus yang lama, return filename baru."""
    if old_filename:
        old_path = os.path.join(AVATAR_FOLDER, old_filename)
        if os.path.exists(old_path):
            os.remove(old_path)
    ext      = file.filename.rsplit('.', 1)[1].lower()
    if ext == 'jpg': ext = 'jpeg'
    filename = f"avatar_{user_id}_{int(time.time())}.{ext}"
    filepath = os.path.join(AVATAR_FOLDER, filename)
    img    = Image.open(file).convert('RGB')
    w, h   = img.size
    size   = min(w, h)
    left   = (w - size) // 2
    top    = (h - size) // 2
    img    = img.crop((left, top, left + size, top + size))
    img    = img.resize((300, 300), Image.LANCZOS)
    img.save(filepath, optimize=True, quality=88)
    return filename


def make_username_from_email(email):
    """Buat username unik dari email Google."""
    base = email.split('@')[0].lower()
    base = ''.join(c for c in base if c.isalnum() or c == '_')[:30]
    if len(base) < 3:
        base = 'user' + base
    candidate = base
    counter = 1
    while db.session.execute(select(User).where(User.username == candidate)).scalar_one_or_none():
        candidate = f"{base}{counter}"
        counter += 1
    return candidate


# ── ROUTES ──
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        nama     = request.form.get('nama','').strip()
        username = request.form.get('username','').strip().lower()
        email    = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        confirm  = request.form.get('confirm_password','')
        errors = []
        if not nama:            errors.append('Nama wajib diisi.')
        if len(username) < 3:   errors.append('Username minimal 3 karakter.')
        if len(password) < 6:   errors.append('Password minimal 6 karakter.')
        if password != confirm: errors.append('Konfirmasi password tidak cocok.')
        if db.session.execute(select(User).where(User.username==username)).scalar_one_or_none():
            errors.append('Username sudah digunakan.')
        if db.session.execute(select(User).where(User.email==email)).scalar_one_or_none():
            errors.append('Email sudah terdaftar.')
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('register.html', nama=nama, username=username, email=email)
        u = User(nama=nama, username=username, email=email)
        u.set_password(password)
        db.session.add(u); db.session.commit()
        # ── Notifikasi: selamat datang → user baru ──
        push_notif(u.id, 'account', '👋 Selamat Datang di RiceScanAI!',
            f'Halo {nama}! Akun Anda berhasil dibuat. Mulai deteksi penyakit padi sekarang.', '/')
        # ── Notifikasi: user baru → semua admin ──
        push_notif_all_admins('new_user', '👤 User Baru Mendaftar',
            f'{nama} (@{username}) baru saja membuat akun.', '/admin/users')
        db.session.commit()
        flash('Registrasi berhasil! Silakan login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form.get('username','').strip().lower()
        password = request.form.get('password','')
        remember = bool(request.form.get('remember'))
        user = db.session.execute(select(User).where(User.username==username)).scalar_one_or_none()
        if not user or not user.check_password(password):
            flash('Username atau password salah.', 'danger')
            return render_template('login.html', username=username)
        login_user(user, remember=remember)
        flash(f'Selamat datang, {user.nama}! 👋', 'success')
        return redirect(request.args.get('next') or url_for('index'))
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Anda telah logout.', 'info')
    return redirect(url_for('login'))


# ── GOOGLE OAUTH ROUTES ──
@app.route('/auth/google')
def google_login():
    """Mulai alur Google OAuth."""
    if not app.config['GOOGLE_CLIENT_ID']:
        flash('Google Login belum dikonfigurasi.', 'warning')
        return redirect(url_for('login'))
    redirect_uri = url_for('google_callback', _external=True)
    # Simpan halaman asal untuk redirect setelah login
    session['oauth_next'] = request.args.get('next', url_for('index'))
    return oauth.google.authorize_redirect(redirect_uri)


@app.route('/auth/google/callback')
def google_callback():
    """Tangani callback dari Google setelah user mengizinkan akses."""
    try:
        token    = oauth.google.authorize_access_token()
        userinfo = token.get('userinfo') or oauth.google.userinfo()
    except Exception as e:
        flash('Login Google gagal. Silakan coba lagi.', 'danger')
        return redirect(url_for('login'))

    google_id   = str(userinfo['sub'])
    email       = userinfo.get('email', '').lower()
    nama        = userinfo.get('name', email.split('@')[0])
    avatar_url  = userinfo.get('picture', '')

    # 1. Cari berdasarkan google_id
    user = db.session.execute(
        select(User).where(User.google_id == google_id)
    ).scalar_one_or_none()

    # 2. Jika belum ada, cari berdasarkan email (akun sudah daftar manual)
    if not user and email:
        user = db.session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user:
            # Tautkan google_id ke akun yang sudah ada
            user.google_id = google_id
            db.session.commit()

    # 3. Buat akun baru jika benar-benar belum ada
    if not user:
        username = make_username_from_email(email)
        user = User(
            nama          = nama,
            username      = username,
            email         = email,
            google_id     = google_id,
            google_avatar = avatar_url,
            password_hash = None,   # tidak perlu password
        )
        user._is_active = True
        db.session.add(user)
        db.session.commit()
        flash(f'Akun berhasil dibuat via Google! Selamat datang, {nama} 🎉', 'success')
    else:
        # Update nama & avatar jika belum ada
        changed = False
        if not user.nama:
            user.nama = nama
            changed = True
        if not user.foto_profil and avatar_url:
            user.google_avatar = avatar_url
            changed = True
        if changed:
            db.session.commit()

    if not user.is_active:
        flash('Akun Anda dinonaktifkan. Hubungi administrator.', 'danger')
        return redirect(url_for('login'))

    login_user(user, remember=True)
    flash(f'Selamat datang, {user.nama}! 👋', 'success')

    next_url = session.pop('oauth_next', url_for('index'))
    return redirect(next_url)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_profile':
            nama  = request.form.get('nama','').strip()
            email = request.form.get('email','').strip().lower()
            if not nama:
                flash('Nama tidak boleh kosong.', 'danger')
            else:
                conflict = db.session.execute(
                    select(User).where(User.email==email, User.id!=current_user.id)
                ).scalar_one_or_none()
                if conflict:
                    flash('Email sudah digunakan akun lain.', 'danger')
                else:
                    current_user.nama = nama; current_user.email = email
                    db.session.commit()
                    flash('Profil berhasil diperbarui! ✅', 'success')
        elif action == 'change_password':
            old_pw  = request.form.get('old_password','')
            new_pw  = request.form.get('new_password','')
            confirm = request.form.get('confirm_new','')
            # Khusus Google user yang belum set password
            if not current_user.password_hash:
                if len(new_pw) < 6:
                    flash('Password baru minimal 6 karakter.', 'danger')
                elif new_pw != confirm:
                    flash('Konfirmasi password tidak cocok.', 'danger')
                else:
                    current_user.set_password(new_pw); db.session.commit()
                    flash('Password berhasil diset! 🔐', 'success')
            else:
                if not current_user.check_password(old_pw):
                    flash('Password lama salah.', 'danger')
                elif len(new_pw) < 6:
                    flash('Password baru minimal 6 karakter.', 'danger')
                elif new_pw != confirm:
                    flash('Konfirmasi password tidak cocok.', 'danger')
                else:
                    current_user.set_password(new_pw); db.session.commit()
                    flash('Password berhasil diubah! 🔐', 'success')
        elif action == 'delete_history':
            db.session.execute(db.delete(RiwayatDeteksi).where(RiwayatDeteksi.user_id==current_user.id))
            db.session.commit()
            flash('Riwayat deteksi berhasil dihapus.', 'info')
        return redirect(url_for('profile'))

    total = count_rows(RiwayatDeteksi, RiwayatDeteksi.user_id==current_user.id)
    riwayat = db.session.execute(
        select(RiwayatDeteksi).where(RiwayatDeteksi.user_id==current_user.id)
        .order_by(RiwayatDeteksi.created_at.desc()).limit(10)
    ).scalars().all()
    stats_raw = db.session.execute(
        select(RiwayatDeteksi.hasil, func.count(RiwayatDeteksi.id))
        .where(RiwayatDeteksi.user_id==current_user.id).group_by(RiwayatDeteksi.hasil)
    ).all()
    return render_template('profile.html', total=total, riwayat=riwayat,
                           stats_dict={r[0]:r[1] for r in stats_raw})


# ── UPLOAD AVATAR (user sendiri) ──
@app.route('/profile/upload-avatar', methods=['POST'])
@login_required
def upload_avatar():
    if 'foto' not in request.files:
        return jsonify({'ok': False, 'error': 'File tidak ditemukan'}), 400
    file = request.files['foto']
    if not file or not allowed_avatar(file.filename):
        return jsonify({'ok': False, 'error': 'Format tidak didukung (jpg/png/webp)'}), 400
    file.seek(0, 2); size = file.tell(); file.seek(0)
    if size > MAX_AVATAR_SIZE:
        return jsonify({'ok': False, 'error': 'Ukuran foto maksimal 2MB'}), 400
    try:
        filename = save_avatar(file, current_user.id, current_user.foto_profil)
        current_user.foto_profil = filename
        db.session.commit()
        return jsonify({'ok': True, 'url': current_user.avatar_url()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── UPLOAD AVATAR (admin untuk user lain) ──
@app.route('/admin/users/<int:user_id>/upload-avatar', methods=['POST'])
@login_required
@admin_required
def admin_upload_avatar(user_id):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User tidak ditemukan'}), 404
    if 'foto' not in request.files:
        return jsonify({'ok': False, 'error': 'File tidak ditemukan'}), 400
    file = request.files['foto']
    if not file or not allowed_avatar(file.filename):
        return jsonify({'ok': False, 'error': 'Format tidak didukung (jpg/png/webp)'}), 400
    file.seek(0, 2); size = file.tell(); file.seek(0)
    if size > MAX_AVATAR_SIZE:
        return jsonify({'ok': False, 'error': 'Ukuran foto maksimal 2MB'}), 400
    try:
        filename = save_avatar(file, user_id, user.foto_profil)
        user.foto_profil = filename
        db.session.commit()
        return jsonify({'ok': True, 'url': user.avatar_url()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    result = error = preview_src = None
    if request.method == 'POST':
        if 'file' not in request.files:
            return render_template('index.html', result=None, error='Form tidak mengirimkan file.', preview_src=None)
        file = request.files['file']
        if not file.filename:
            return render_template('index.html', result=None, error='Silakan pilih gambar terlebih dahulu.', preview_src=None)
        try:
            file.seek(0, 2); size = file.tell(); file.seek(0)
            if size > MAX_FILE_SIZE:
                return render_template('index.html', result=None, error=f'File terlalu besar ({size//1024} KB). Maks 5 MB.', preview_src=None)
            preview_src = image_to_base64(file)
            file.seek(0)
            preds           = get_model().predict(preprocess_image(file), verbose=0)
            predicted_index = int(np.argmax(preds))
            confidence      = float(np.max(preds))
            sorted_p        = np.sort(preds[0])[::-1]
            margin          = float(sorted_p[0]) - float(sorted_p[1])
            print(f'[predict] conf={confidence:.3f} margin={margin:.3f}')
            if confidence < CONFIDENCE_THRESHOLD or margin < MARGIN_THRESHOLD:
                result = {'label':'Tidak Dikenali','emoji':'⚠️',
                          'desc':'Gambar tidak dikenali sebagai daun padi yang sakit.',
                          'saran':'Gunakan foto daun padi yang jelas, fokus, pencahayaan cukup.',
                          'confidence':round(confidence*100,2)}
            else:
                info   = disease_info[class_names[predicted_index]]
                result = {**info, 'confidence': round(confidence*100, 2)}
            db.session.add(RiwayatDeteksi(
                user_id=current_user.id, hasil=result['label'], emoji=result['emoji'],
                confidence=result['confidence'], preview_b64=preview_src))
            db.session.commit()

            # ── Notifikasi: scan selesai → user ──
            label_lower = result['label'].lower()
            if label_lower == 'tidak dikenali':
                notif_title = '⚠️ Deteksi Tidak Dikenali'
                notif_msg   = f'Gambar tidak dapat diidentifikasi. Confidence: {result["confidence"]}%. Coba foto yang lebih jelas.'
            else:
                emoji_map = {'blast':'🔴','blight':'🟡','tungro':'🔵'}
                em = emoji_map.get(label_lower, '🌿')
                notif_title = f'{em} Deteksi Selesai: {result["label"]}'
                notif_msg   = f'Penyakit terdeteksi dengan confidence {result["confidence"]}%. Lihat saran penanganan di riwayat.'
            push_notif(current_user.id, 'scan', notif_title, notif_msg, '/riwayat')
            # ── Notifikasi: deteksi baru → semua admin ──
            push_notif_all_admins('new_scan', '🔬 Deteksi Baru',
                f'{current_user.nama} mendeteksi: {result["label"]} ({result["confidence"]}%)',
                '/admin')
            db.session.commit()
        except Exception as e:
            error = f'Gagal memproses gambar: {e}'
            print(f'[ERROR] {e}')
            return render_template('index.html', result=None, error=error, preview_src=preview_src)
    return render_template('index.html', result=result, error=error, preview_src=preview_src)


@app.route('/riwayat')
@login_required
def riwayat():
    page         = request.args.get('page', 1, type=int)
    filter_hasil = request.args.get('filter', 'semua')
    stmt = select(RiwayatDeteksi).where(RiwayatDeteksi.user_id==current_user.id)
    filters = {'blast':'%blast%','blight':'%blight%','tungro':'%tungro%','tidak_dikenali':'%tidak%'}
    if filter_hasil in filters:
        stmt = stmt.where(RiwayatDeteksi.hasil.ilike(filters[filter_hasil]))
    stmt = stmt.order_by(RiwayatDeteksi.created_at.desc())
    pagination = run_paginate(stmt, page, 12)
    total = count_rows(RiwayatDeteksi, RiwayatDeteksi.user_id==current_user.id)
    stats_raw = db.session.execute(
        select(RiwayatDeteksi.hasil, func.count(RiwayatDeteksi.id))
        .where(RiwayatDeteksi.user_id==current_user.id).group_by(RiwayatDeteksi.hasil)
    ).all()
    return render_template('riwayat.html', items=pagination.items, pagination=pagination,
                           filter_hasil=filter_hasil, total=total, stats_dict={r[0]:r[1] for r in stats_raw})


@app.route('/riwayat/hapus/<int:item_id>', methods=['POST'])
@login_required
def hapus_riwayat_item(item_id):
    item = db.session.execute(
        select(RiwayatDeteksi).where(RiwayatDeteksi.id==item_id, RiwayatDeteksi.user_id==current_user.id)
    ).scalar_one_or_none()
    if not item: return 'Not found', 404
    db.session.delete(item); db.session.commit()
    flash('Riwayat berhasil dihapus.', 'success')
    return redirect(request.referrer or url_for('riwayat'))


# ── API: GRAFIK RIWAYAT (realtime) ──
@app.route('/api/riwayat-grafik')
@login_required
def api_riwayat_grafik():
    try:
        days = request.args.get('days', 7, type=int)
        if days not in (7, 30, 90):
            days = 7

        cutoff = now_wib() - timedelta(days=days)

        def cat(hasil):
            h = (hasil or '').lower()
            if 'blast'  in h: return 'blast'
            if 'blight' in h or 'hawar' in h: return 'blight'
            if 'tungro' in h: return 'tungro'
            return 'sehat'

        def lbl(dt):
            return '{}/{}'.format(dt.day, str(dt.month).zfill(2))

        bucket_count = days if days <= 7 else (7 if days <= 30 else 6)
        step = days / bucket_count
        buckets = []
        for i in range(bucket_count):
            mid = cutoff + timedelta(days=i * step + step / 2)
            buckets.append({'date': mid.strftime('%Y-%m-%d'), 'label': lbl(mid),
                            'blast': 0, 'blight': 0, 'tungro': 0, 'sehat': 0})

        rows = db.session.execute(
            select(RiwayatDeteksi.hasil, RiwayatDeteksi.created_at)
            .where(RiwayatDeteksi.user_id == current_user.id,
                   RiwayatDeteksi.created_at >= cutoff)
            .order_by(RiwayatDeteksi.created_at.asc())
        ).all()

        for hasil, created_at in rows:
            c = cat(hasil)
            best = min(buckets,
                key=lambda b: abs((created_at - datetime.strptime(b['date'], '%Y-%m-%d')).total_seconds()))
            best[c] += 1

        all_h = db.session.execute(
            select(RiwayatDeteksi.hasil)
            .where(RiwayatDeteksi.user_id == current_user.id)
        ).scalars().all()

        totals = {'blast': 0, 'blight': 0, 'tungro': 0, 'sehat': 0}
        for h in all_h:
            totals[cat(h)] += 1
        grand = sum(totals.values())

        last_dt = db.session.scalar(
            select(RiwayatDeteksi.created_at)
            .where(RiwayatDeteksi.user_id == current_user.id)
            .order_by(RiwayatDeteksi.created_at.desc())
            .limit(1)
        )

        most = max(totals, key=totals.get) if grand else None
        most_lbl   = {'blast':'Blast','blight':'Blight','tungro':'Tungro','sehat':'Sehat'}
        most_emoji = {'blast':'🔴','blight':'🟡','tungro':'🔵','sehat':'🌿'}
        max_b = max((b['blast']+b['blight']+b['tungro']+b['sehat'] for b in buckets), default=1) or 1

        return jsonify({
            'ok': True, 'days': days, 'buckets': buckets,
            'totals': totals, 'grand_total': grand, 'max_bucket': max_b,
            'last_scan':    last_dt.strftime('%d %b %Y, %H:%M') if last_dt else None,
            'most_common': (most_emoji.get(most,'') + ' ' + most_lbl.get(most,'')) if most else None,
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── ADMIN ──
@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    stats_raw = db.session.execute(
        select(RiwayatDeteksi.hasil, func.count(RiwayatDeteksi.id)).group_by(RiwayatDeteksi.hasil)
    ).all()
    users_baru = db.session.execute(
        select(User).where(User.role=='user').order_by(User.created_at.desc()).limit(5)
    ).scalars().all()
    deteksi_baru = db.session.execute(
        select(RiwayatDeteksi).order_by(RiwayatDeteksi.created_at.desc()).limit(10)
    ).scalars().all()
    return render_template('admin/dashboard.html',
        total_users   = count_rows(User, User.role=='user'),
        total_admins  = count_rows(User, User.role=='admin'),
        total_deteksi = count_rows(RiwayatDeteksi),
        user_aktif    = count_rows(User, User._is_active==True,  User.role=='user'),
        user_nonaktif = count_rows(User, User._is_active==False, User.role=='user'),
        stats_dict    = {r[0]:r[1] for r in stats_raw},
        users_baru    = users_baru,
        deteksi_baru  = deteksi_baru)


@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    page          = request.args.get('page', 1, type=int)
    search_q      = request.args.get('q', '').strip()
    filter_role   = request.args.get('role', 'semua')
    filter_status = request.args.get('status', 'semua')
    stmt = select(User)
    if search_q:
        stmt = stmt.where(or_(
            User.nama.ilike(f'%{search_q}%'),
            User.username.ilike(f'%{search_q}%'),
            User.email.ilike(f'%{search_q}%')))
    if filter_role   == 'admin':      stmt = stmt.where(User.role=='admin')
    elif filter_role == 'user':       stmt = stmt.where(User.role=='user')
    if filter_status == 'aktif':      stmt = stmt.where(User._is_active==True)
    elif filter_status == 'nonaktif': stmt = stmt.where(User._is_active==False)
    stmt = stmt.order_by(User.created_at.desc())
    pagination = run_paginate(stmt, page, 15)
    return render_template('admin/users.html',
        users=pagination.items, pagination=pagination,
        search=search_q, filter_role=filter_role, filter_status=filter_status)


@app.route('/admin/users/tambah', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_tambah_user():
    if request.method == 'POST':
        nama     = request.form.get('nama','').strip()
        username = request.form.get('username','').strip().lower()
        email    = request.form.get('email','').strip().lower()
        password = request.form.get('password','')
        role     = request.form.get('role','user')
        errors = []
        if not nama:          errors.append('Nama wajib diisi.')
        if len(username) < 3: errors.append('Username minimal 3 karakter.')
        if len(password) < 6: errors.append('Password minimal 6 karakter.')
        if db.session.execute(select(User).where(User.username==username)).scalar_one_or_none():
            errors.append('Username sudah digunakan.')
        if db.session.execute(select(User).where(User.email==email)).scalar_one_or_none():
            errors.append('Email sudah terdaftar.')
        if errors:
            for e in errors: flash(e, 'danger')
            return render_template('admin/tambah_user.html', nama=nama, username=username, email=email, role=role)
        u = User(nama=nama, username=username, email=email, role=role)
        u.set_password(password)
        db.session.add(u); db.session.commit()
        # ── Notifikasi: selamat datang → user baru dibuat admin ──
        role_lbl = 'Administrator' if role == 'admin' else 'User'
        push_notif(u.id, 'account', '👋 Akun Anda Telah Dibuat',
            f'Akun {role_lbl} Anda dibuat oleh admin. Selamat bergabung di RiceScanAI!', '/')
        db.session.commit()
        flash(f'User @{username} berhasil ditambahkan.', 'success')
        return redirect(url_for('admin_users'))
    return render_template('admin/tambah_user.html')


@app.route('/admin/users/<int:user_id>')
@login_required
@admin_required
def admin_user_detail(user_id):
    user = db.session.get(User, user_id)
    if not user:
        flash('User tidak ditemukan.', 'danger')
        return redirect(url_for('admin_users'))
    riwayat = db.session.execute(
        select(RiwayatDeteksi).where(RiwayatDeteksi.user_id==user_id)
        .order_by(RiwayatDeteksi.created_at.desc()).limit(20)
    ).scalars().all()
    stats_raw = db.session.execute(
        select(RiwayatDeteksi.hasil, func.count(RiwayatDeteksi.id))
        .where(RiwayatDeteksi.user_id==user_id).group_by(RiwayatDeteksi.hasil)
    ).all()
    return render_template('admin/user_detail.html', user=user, riwayat=riwayat,
        total_deteksi=count_rows(RiwayatDeteksi, RiwayatDeteksi.user_id==user_id),
        stats_dict={r[0]:r[1] for r in stats_raw})


@app.route('/admin/users/<int:user_id>/toggle-status', methods=['POST'])
@login_required
@admin_required
def admin_toggle_status(user_id):
    user = db.session.get(User, user_id)
    if not user: flash('User tidak ditemukan.', 'danger'); return redirect(url_for('admin_users'))
    if user.id == current_user.id:
        flash('Tidak dapat mengubah status akun sendiri.', 'warning')
        return redirect(request.referrer or url_for('admin_users'))
    user.is_active = not user.is_active
    db.session.commit()
    # ── Notifikasi: perubahan status → user ──
    if user.is_active:
        push_notif(user.id, 'account', '✅ Akun Anda Diaktifkan',
            'Akun Anda telah diaktifkan kembali oleh admin. Anda bisa login sekarang.', '/profile')
    else:
        push_notif(user.id, 'account', '🚫 Akun Anda Dinonaktifkan',
            'Akun Anda telah dinonaktifkan oleh admin. Hubungi admin untuk informasi lebih lanjut.', '/chat')
    db.session.commit()
    flash(f'Akun @{user.username} berhasil {"diaktifkan" if user.is_active else "dinonaktifkan"}.', 'success')
    next_url = request.form.get('next')
    return redirect(next_url or request.referrer or url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/toggle-role', methods=['POST'])
@login_required
@admin_required
def admin_toggle_role(user_id):
    user = db.session.get(User, user_id)
    if not user: flash('User tidak ditemukan.', 'danger'); return redirect(url_for('admin_users'))
    if user.id == current_user.id:
        flash('Tidak dapat mengubah role akun sendiri.', 'warning')
        return redirect(request.referrer or url_for('admin_users'))
    user.role = 'admin' if user.role == 'user' else 'user'
    db.session.commit()
    # ── Notifikasi: perubahan role → user ──
    role_label = 'Administrator' if user.role == 'admin' else 'User Biasa'
    push_notif(user.id, 'account', '🔄 Role Akun Diubah',
        f'Role akun Anda telah diubah menjadi {role_label} oleh admin.', '/profile')
    db.session.commit()
    flash(f'Role @{user.username} diubah menjadi {user.role}.', 'success')
    next_url = request.form.get('next')
    return redirect(next_url or request.referrer or url_for('admin_users'))


@app.route('/admin/users/<int:user_id>/reset-password', methods=['POST'])
@login_required
@admin_required
def admin_reset_password(user_id):
    user = db.session.get(User, user_id)
    if not user: flash('User tidak ditemukan.', 'danger'); return redirect(url_for('admin_users'))
    new_pass = request.form.get('new_password','').strip()
    if len(new_pass) < 6:
        flash('Password minimal 6 karakter.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user_id))
    user.set_password(new_pass); db.session.commit()
    flash(f'Password @{user.username} berhasil direset.', 'success')
    return redirect(url_for('admin_user_detail', user_id=user_id))


@app.route('/admin/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if not user: flash('User tidak ditemukan.', 'danger'); return redirect(url_for('admin_users'))
    if user.id == current_user.id:
        flash('Tidak dapat menghapus akun sendiri.', 'warning')
        return redirect(url_for('admin_users'))
    if user.foto_profil:
        old_path = os.path.join(AVATAR_FOLDER, user.foto_profil)
        if os.path.exists(old_path): os.remove(old_path)
    username = user.username
    db.session.delete(user); db.session.commit()
    flash(f'Akun @{username} berhasil dihapus.', 'success')
    next_url = request.form.get('next')
    return redirect(next_url or url_for('admin_users'))


# ──────────────────────────────────────────────
# ── CHAT ──
# ──────────────────────────────────────────────

@app.route('/chat')
@login_required
def chat():
    """Halaman chat user dengan admin."""
    messages = ChatMessage.query.filter_by(user_id=current_user.id)\
                .order_by(ChatMessage.created_at.asc()).all()
    # Mark admin messages as read
    ChatMessage.query.filter_by(user_id=current_user.id, sender='admin', is_read=False)\
               .update({'is_read': True})
    db.session.commit()
    return render_template('chat.html', messages=messages)


@app.route('/chat/send', methods=['POST'])
@login_required
def chat_send():
    """User kirim pesan ke admin."""
    msg_text = request.form.get('message', '').strip()
    if not msg_text or len(msg_text) > 1000:
        return jsonify({'ok': False, 'error': 'Pesan tidak valid'}), 400
    msg = ChatMessage(user_id=current_user.id, sender='user', message=msg_text)
    db.session.add(msg)
    db.session.commit()
    # ── Notifikasi: pesan baru user → semua admin ──
    preview = msg_text[:60] + ('...' if len(msg_text) > 60 else '')
    push_notif_all_admins('chat', '💬 Pesan Baru dari User',
        f'{current_user.nama}: "{preview}"',
        f'/admin/chat/{current_user.id}')
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id,
                    'message': msg.message,
                    'created_at': msg.created_at.strftime('%H:%M')})


@app.route('/chat/poll')
@login_required
def chat_poll():
    """Polling pesan baru untuk user (long-poll ringan)."""
    since_id = request.args.get('since', 0, type=int)
    msgs = ChatMessage.query.filter(
        ChatMessage.user_id == current_user.id,
        ChatMessage.id > since_id
    ).order_by(ChatMessage.created_at.asc()).all()
    # Mark new admin messages as read
    for m in msgs:
        if m.sender == 'admin' and not m.is_read:
            m.is_read = True
    db.session.commit()
    return jsonify([{'id': m.id, 'sender': m.sender,
                     'message': m.message,
                     'created_at': m.created_at.strftime('%H:%M')} for m in msgs])


# ── ADMIN CHAT ──

@app.route('/admin/chat')
@login_required
def admin_chat_list():
    """Admin: daftar semua user yang punya percakapan."""
    admin_required_inner = current_user.is_admin
    if not admin_required_inner:
        return redirect(url_for('index'))
    # Ambil user yang pernah chat, urutkan berdasarkan pesan terakhir
    user_ids = db.session.execute(
        text('SELECT DISTINCT user_id FROM chat_messages ORDER BY id DESC')
    ).fetchall()
    users_with_chat = []
    for row in user_ids:
        u = db.session.get(User, row[0])
        if u:
            last_msg = ChatMessage.query.filter_by(user_id=u.id)\
                       .order_by(ChatMessage.created_at.desc()).first()
            unread = ChatMessage.query.filter_by(user_id=u.id, sender='user', is_read=False).count()
            users_with_chat.append({'user': u, 'last_msg': last_msg, 'unread': unread})
    return render_template('admin/chat_list.html', users_with_chat=users_with_chat)


@app.route('/admin/chat/<int:user_id>')
@login_required
def admin_chat_detail(user_id):
    """Admin: percakapan dengan user tertentu."""
    if not current_user.is_admin:
        return redirect(url_for('index'))
    target = db.session.get(User, user_id)
    if not target:
        return redirect(url_for('admin_chat_list'))
    messages = ChatMessage.query.filter_by(user_id=user_id)\
               .order_by(ChatMessage.created_at.asc()).all()
    # Mark user messages as read
    ChatMessage.query.filter_by(user_id=user_id, sender='user', is_read=False)\
               .update({'is_read': True})
    db.session.commit()
    return render_template('admin/chat_detail.html', target=target, messages=messages)


@app.route('/admin/chat/<int:user_id>/send', methods=['POST'])
@login_required
def admin_chat_send(user_id):
    """Admin kirim balasan ke user."""
    if not current_user.is_admin:
        return jsonify({'ok': False}), 403
    msg_text = request.form.get('message', '').strip()
    if not msg_text or len(msg_text) > 1000:
        return jsonify({'ok': False}), 400
    msg = ChatMessage(user_id=user_id, sender='admin', message=msg_text)
    db.session.add(msg)
    db.session.commit()
    # ── Notifikasi: balasan admin → user ──
    preview = msg_text[:60] + ('...' if len(msg_text) > 60 else '')
    push_notif(user_id, 'chat', '💬 Pesan Baru dari Admin',
        f'Admin membalas: "{preview}"', '/chat')
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id,
                    'message': msg.message,
                    'created_at': msg.created_at.strftime('%H:%M')})


@app.route('/admin/chat/<int:user_id>/poll')
@login_required
def admin_chat_poll(user_id):
    """Polling pesan baru untuk admin."""
    if not current_user.is_admin:
        return jsonify([]), 403
    since_id = request.args.get('since', 0, type=int)
    msgs = ChatMessage.query.filter(
        ChatMessage.user_id == user_id,
        ChatMessage.id > since_id
    ).order_by(ChatMessage.created_at.asc()).all()
    for m in msgs:
        if m.sender == 'user' and not m.is_read:
            m.is_read = True
    db.session.commit()
    return jsonify([{'id': m.id, 'sender': m.sender,
                     'message': m.message,
                     'created_at': m.created_at.strftime('%H:%M')} for m in msgs])


@app.route('/admin/chat/unread-count')
@login_required
def admin_chat_unread():
    """Jumlah total pesan belum dibaca untuk badge di navbar admin."""
    if not current_user.is_admin:
        return jsonify({'count': 0})
    count = ChatMessage.query.filter_by(sender='user', is_read=False).count()
    return jsonify({'count': count})


@app.route('/chat/unread-count')
@login_required
def user_chat_unread():
    """Jumlah pesan admin belum dibaca untuk badge di navbar user."""
    count = ChatMessage.query.filter_by(
        user_id=current_user.id, sender='admin', is_read=False).count()
    return jsonify({'count': count})




# ── CHAT CRUD: Edit & Delete (user) ──
@app.route('/chat/message/<int:msg_id>/edit', methods=['POST'])
@login_required
def chat_edit(msg_id):
    """User edit pesan miliknya sendiri."""
    msg = db.session.get(ChatMessage, msg_id)
    if not msg or msg.user_id != current_user.id or msg.sender != 'user':
        return jsonify({'ok': False, 'error': 'Tidak ditemukan'}), 404
    new_text = request.form.get('message', '').strip()
    if not new_text or len(new_text) > 1000:
        return jsonify({'ok': False, 'error': 'Pesan tidak valid'}), 400
    msg.message = new_text
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id, 'message': msg.message})


@app.route('/chat/message/<int:msg_id>/delete', methods=['POST'])
@login_required
def chat_delete(msg_id):
    """User hapus pesan miliknya sendiri."""
    msg = db.session.get(ChatMessage, msg_id)
    if not msg or msg.user_id != current_user.id or msg.sender != 'user':
        return jsonify({'ok': False, 'error': 'Tidak ditemukan'}), 404
    db.session.delete(msg)
    db.session.commit()
    return jsonify({'ok': True, 'id': msg_id})


# ── ADMIN CHAT CRUD: Edit & Delete ──
@app.route('/admin/chat/message/<int:msg_id>/edit', methods=['POST'])
@login_required
@admin_required
def admin_chat_edit(msg_id):
    """Admin edit pesan miliknya (sender=admin)."""
    msg = db.session.get(ChatMessage, msg_id)
    if not msg or msg.sender != 'admin':
        return jsonify({'ok': False, 'error': 'Tidak ditemukan'}), 404
    new_text = request.form.get('message', '').strip()
    if not new_text or len(new_text) > 1000:
        return jsonify({'ok': False, 'error': 'Pesan tidak valid'}), 400
    msg.message = new_text
    db.session.commit()
    return jsonify({'ok': True, 'id': msg.id, 'message': msg.message})


@app.route('/admin/chat/message/<int:msg_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_chat_delete(msg_id):
    """Admin hapus pesan apapun (milik admin maupun user)."""
    msg = db.session.get(ChatMessage, msg_id)
    if not msg:
        return jsonify({'ok': False, 'error': 'Tidak ditemukan'}), 404
    db.session.delete(msg)
    db.session.commit()
    return jsonify({'ok': True, 'id': msg_id})



@app.route('/admin/chat/<int:user_id>/delete-all', methods=['POST'])
@login_required
@admin_required
def admin_chat_delete_all(user_id):
    """Admin hapus seluruh percakapan dengan user tertentu."""
    ChatMessage.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    return jsonify({'ok': True})


# ── LAPORAN USER ──
@app.route('/laporan')
@login_required
def laporan_user():
    """Laporan deteksi pribadi user."""
    total = count_rows(RiwayatDeteksi, RiwayatDeteksi.user_id == current_user.id)
    riwayat_all = db.session.execute(
        select(RiwayatDeteksi)
        .where(RiwayatDeteksi.user_id == current_user.id)
        .order_by(RiwayatDeteksi.created_at.desc())
    ).scalars().all()
    stats_raw = db.session.execute(
        select(RiwayatDeteksi.hasil, func.count(RiwayatDeteksi.id))
        .where(RiwayatDeteksi.user_id == current_user.id)
        .group_by(RiwayatDeteksi.hasil)
    ).all()
    avg_conf_raw = db.session.scalar(
        select(func.avg(RiwayatDeteksi.confidence))
        .where(RiwayatDeteksi.user_id == current_user.id)
    )
    avg_conf = round(avg_conf_raw, 1) if avg_conf_raw else None
    return render_template(
        'laporan_user.html',
        total       = total,
        riwayat     = riwayat_all,
        stats_dict  = {r[0]: r[1] for r in stats_raw},
        avg_conf    = avg_conf,
        now         = now_wib(),
    )


# ── LAPORAN ADMIN ──
@app.route('/admin/laporan')
@login_required
@admin_required
def admin_laporan():
    """Laporan lengkap semua user untuk admin."""
    stats_raw = db.session.execute(
        select(RiwayatDeteksi.hasil, func.count(RiwayatDeteksi.id))
        .group_by(RiwayatDeteksi.hasil)
    ).all()
    users_all = db.session.execute(
        select(User).order_by(User.created_at.desc())
    ).scalars().all()
    recent_det = db.session.execute(
        select(RiwayatDeteksi)
        .order_by(RiwayatDeteksi.created_at.desc())
        .limit(50)
    ).scalars().all()
    avg_conf_raw = db.session.scalar(select(func.avg(RiwayatDeteksi.confidence)))
    avg_conf = round(avg_conf_raw, 1) if avg_conf_raw else None
    # Hitung deteksi per user
    det_rows = db.session.execute(
        select(RiwayatDeteksi.user_id, func.count(RiwayatDeteksi.id))
        .group_by(RiwayatDeteksi.user_id)
    ).all()
    user_det = {row[0]: row[1] for row in det_rows}
    return render_template(
        'admin/laporan.html',
        total_users   = count_rows(User, User.role == 'user'),
        total_admins  = count_rows(User, User.role == 'admin'),
        total_deteksi = count_rows(RiwayatDeteksi),
        user_aktif    = count_rows(User, User._is_active == True,  User.role == 'user'),
        user_nonaktif = count_rows(User, User._is_active == False, User.role == 'user'),
        stats_dict    = {r[0]: r[1] for r in stats_raw},
        users         = users_all,
        recent_det    = recent_det,
        avg_conf      = avg_conf,
        user_det      = user_det,
        now           = now_wib(),
    )




# ════════════════════════════════════════════════
# ── NOTIFICATION API ──
# ════════════════════════════════════════════════

@app.route('/api/notifications')
@login_required
def api_notifications():
    """Ambil notifikasi untuk user yang sedang login."""
    notifs = Notification.query.filter_by(user_id=current_user.id)\
             .order_by(Notification.created_at.desc()).limit(20).all()
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({
        'unread': unread,
        'items': [{
            'id':       n.id,
            'type':     n.type,
            'title':    n.title,
            'message':  n.message,
            'link':     n.link or '/',
            'is_read':  n.is_read,
            'time_ago': time_ago(n.created_at),
        } for n in notifs]
    })


@app.route('/api/notifications/<int:nid>/read', methods=['POST'])
@login_required
def api_notif_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first()
    if n:
        n.is_read = True
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def api_notif_read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/notifications/<int:nid>/delete', methods=['POST'])
@login_required
def api_notif_delete(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first()
    if n:
        db.session.delete(n)
        db.session.commit()
    return jsonify({'ok': True})


# Admin notification endpoints (same user_id filter — admin is just a user with role=admin)
@app.route('/admin/api/notifications')
@login_required
@admin_required
def admin_api_notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
             .order_by(Notification.created_at.desc()).limit(20).all()
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({
        'unread': unread,
        'items': [{
            'id':       n.id,
            'type':     n.type,
            'title':    n.title,
            'message':  n.message,
            'link':     n.link or '/admin',
            'is_read':  n.is_read,
            'time_ago': time_ago(n.created_at),
        } for n in notifs]
    })


@app.route('/admin/api/notifications/<int:nid>/read', methods=['POST'])
@login_required
@admin_required
def admin_api_notif_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first()
    if n:
        n.is_read = True
        db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/api/notifications/read-all', methods=['POST'])
@login_required
@admin_required
def admin_api_notif_read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/admin/api/notifications/<int:nid>/delete', methods=['POST'])
@login_required
@admin_required
def admin_api_notif_delete(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first()
    if n:
        db.session.delete(n)
        db.session.commit()
    return jsonify({'ok': True})


# ── INIT DB ──
with app.app_context():
    db.create_all()  # creates chat_messages + notifications tables automatically
    inspector = sa_inspect(db.engine)
    existing  = [c['name'] for c in inspector.get_columns('users')]
    with db.engine.connect() as conn:
        if 'role' not in existing:
            conn.execute(text('ALTER TABLE users ADD COLUMN role VARCHAR(20) DEFAULT "user" NOT NULL'))
            conn.commit(); print('✅ Kolom role ditambahkan')
        if 'is_active' not in existing:
            conn.execute(text('ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1 NOT NULL'))
            conn.commit(); print('✅ Kolom is_active ditambahkan')
        if 'foto_profil' not in existing:
            conn.execute(text('ALTER TABLE users ADD COLUMN foto_profil VARCHAR(120)'))
            conn.commit(); print('✅ Kolom foto_profil ditambahkan')
        if 'google_id' not in existing:
            conn.execute(text('ALTER TABLE users ADD COLUMN google_id VARCHAR(120)'))
            conn.commit(); print('✅ Kolom google_id ditambahkan')
        if 'google_avatar' not in existing:
            conn.execute(text('ALTER TABLE users ADD COLUMN google_avatar VARCHAR(500)'))
            conn.commit(); print('✅ Kolom google_avatar ditambahkan')

    admin = db.session.execute(select(User).where(User.username=='admin')).scalar_one_or_none()
    if not admin:
        admin = User(nama='Administrator', username='admin', email='admin@ricescan.local', role='admin')
        admin._is_active = True
        admin.set_password('admin123')
        db.session.add(admin); db.session.commit()
        print('✅ Akun admin dibuat: admin / admin123')
    elif admin.role != 'admin':
        admin.role = 'admin'; db.session.commit(); print('✅ Role admin diupgrade')
    print('✅ Database siap')

if __name__ == '__main__':
    print('🚀 RiceScanAI starting...')
    port  = int(os.environ.get('PORT', 8000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
