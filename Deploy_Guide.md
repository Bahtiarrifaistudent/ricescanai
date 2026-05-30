# 🚀 PANDUAN DEPLOY RICESCANAI KE RENDER.COM

## ⚠️ Disesuaikan dengan kondisi repo kamu sekarang

---

## 🔍 KONDISI REPO KAMU SAAT INI

Repo kamu di `github.com/Bahtiarrifaistudent/ricescanai` saat ini isinya:

```
ricescanai/          ← ROOT repo (ini yang di GitHub)
├── static/
├── templates/
├── app.py
├── migrate.py
└── ricescan.db      ← ⚠️ HARUS DIHAPUS dari repo!
```

**Masalah yang perlu diperbaiki:**

1. ❌ Tidak ada folder `model/` → file `.keras` belum ada di repo
2. ❌ Tidak ada `Procfile`, `render.yaml`, `requirements.txt`, `.gitignore`
3. ❌ File `ricescan.db` (database lokal) ikut ke-commit — harus dihapus
4. ❌ Struktur berbeda dari `app.py` yang mengharapkan path `../model/rice_leaf_model.keras`

---

## 🛠️ LANGKAH 0 — Perbaiki Struktur Repo (WAJIB DULU)

Buka terminal VS Code di folder project kamu (`RICE-LEAF-DISEASE` atau folder root lokal).

### 0a. Hapus `ricescan.db` dari Git tracking

```bash
git rm --cached ricescan.db
```

### 0b. Tambahkan `.gitignore` dulu sebelum apapun

Salin file `.gitignore` yang sudah disiapkan ke ROOT folder project kamu (sejajar `app.py`).
Lalu jalankan:

```bash
git add .gitignore
git commit -m "fix: add .gitignore and remove db from tracking"
git push
```

---

## 📁 LANGKAH 1 — Rapikan Struktur Folder

Karena `app.py` kamu ada langsung di root repo (bukan di folder `webapp/`),
kita perlu **menyesuaikan `render.yaml` dan `Procfile`** agar cocok.

Struktur target kamu adalah seperti ini:

```
ricescanai/                     ← ROOT repo (sudah ada di GitHub)
│
├── model/
│   └── rice_leaf_model.keras   ← ⚠️ WAJIB ADA, tambahkan!
│
├── static/                     ← sudah ada ✅
├── templates/                  ← sudah ada ✅
├── app.py                      ← sudah ada ✅
├── migrate.py                  ← sudah ada ✅
│
├── .gitignore                  ← tambahkan (file baru)
├── Procfile                    ← tambahkan (file baru, VERSI BARU)
├── render.yaml                 ← tambahkan (file baru, VERSI BARU)
├── requirements.txt            ← tambahkan (file baru)
└── README.md                   ← opsional
```

> 💡 Karena `app.py` ada di root (bukan di `webapp/`), gunakan file
> `Procfile` dan `render.yaml` **VERSI BARU** di bawah ini — bukan yang lama!

---

## 📄 LANGKAH 2 — Salin File-File Baru ke Root Folder

Download semua file yang sudah disiapkan, lalu letakkan di root folder repo kamu.

### `Procfile` (VERSI BARU — sesuai struktur root)

```
web: gunicorn app:app --workers 1 --timeout 120 --bind 0.0.0.0:$PORT
```

> Perhatikan: `app:app` bukan `webapp.app:app` karena `app.py` ada di root!

### `render.yaml` (VERSI BARU — sesuai struktur root)

```yaml
services:
  - type: web
    name: ricescanai
    env: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn app:app --workers 1 --timeout 120 --bind 0.0.0.0:$PORT
    envVars:
      - key: SECRET_KEY
        generateValue: true
      - key: GOOGLE_CLIENT_ID
        sync: false
      - key: GOOGLE_CLIENT_SECRET
        sync: false
      - key: DATABASE_URL
        fromDatabase:
          name: ricescanai-db
          property: connectionString
      - key: MODEL_PATH
        value: ./model/rice_leaf_model.keras
      - key: FLASK_DEBUG
        value: "false"
      - key: PYTHON_VERSION
        value: "3.11.0"

databases:
  - name: ricescanai-db
    plan: free
```

---

## 📦 LANGKAH 3 — Tambahkan File Model

### Cek ukuran model dulu:

```bash
ls -lh /path/ke/model/rice_leaf_model.keras
```

### Jika model ≤ 100MB — copy biasa:

```bash
# Buat folder model di dalam repo
mkdir model
copy "C:\ML\rice-leaf-disease\model\rice_leaf_model.keras" model\
```

### Jika model > 100MB — WAJIB pakai Git LFS:

```bash
# 1. Install Git LFS (sekali saja)
# Download: https://git-lfs.com atau via winget:
winget install GitHub.GitLFS

# 2. Setup LFS di repo
git lfs install
git lfs track "*.keras"
git add .gitattributes

# 3. Buat folder dan copy model
mkdir model
copy "C:\ML\rice-leaf-disease\model\rice_leaf_model.keras" model\
```

---

## 🐙 LANGKAH 4 — Update MODEL_PATH di app.py

Buka `app.py`, cari baris ini (sekitar baris 41-44):

```python
MODEL_PATH = os.environ.get(
    'MODEL_PATH',
    os.path.join(BASE_DIR, '..', 'model', 'rice_leaf_model.keras')
)
```

Ubah path fallback-nya karena sekarang `model/` sejajar dengan `app.py`:

```python
MODEL_PATH = os.environ.get(
    'MODEL_PATH',
    os.path.join(BASE_DIR, 'model', 'rice_leaf_model.keras')
)
```

> Hapus `'..'` — karena `model/` sekarang ada di folder yang sama dengan `app.py`

---

## 🚀 LANGKAH 5 — Push Semua Perubahan ke GitHub

```bash
# Pastikan kamu di root folder repo (lokasi app.py)
git add .
git commit -m "feat: add model, config files, fix structure for Render deploy"
git push
```

Verifikasi di GitHub bahwa repo sudah ada:

- ✅ Folder `model/` dengan file `.keras` di dalamnya
- ✅ File `Procfile`, `render.yaml`, `requirements.txt`, `.gitignore`
- ✅ File `ricescan.db` sudah **tidak ada** lagi

---

## 🌐 LANGKAH 6 — Setup di Render.com

1. Buka https://render.com → login
2. **New +** → **Web Service**
3. Connect repo `Bahtiarrifaistudent/ricescanai`
4. Isi pengaturan:
   - **Region**: Singapore
   - **Branch**: main
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --workers 1 --timeout 120 --bind 0.0.0.0:$PORT`
   - **Plan**: Free
5. **Jangan deploy dulu** → lanjut ke Langkah 7

---

## 🗄️ LANGKAH 7 — Buat Database PostgreSQL di Render

1. **New +** → **PostgreSQL**
2. Nama: `ricescanai-db`, Plan: **Free**
3. Klik **Create Database** → tunggu status **Available**
4. Klik database → copy **Internal Database URL**

---

## 🔑 LANGKAH 8 — Isi Environment Variables di Render

Web Service → tab **Environment** → **Add Environment Variable**:

| Key                      | Value                                                                 |
| ------------------------ | --------------------------------------------------------------------- |
| `SECRET_KEY`           | Generate:`python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL`         | Paste Internal Database URL dari Langkah 7                            |
| `GOOGLE_CLIENT_ID`     | Dari Google Cloud Console                                             |
| `GOOGLE_CLIENT_SECRET` | Dari Google Cloud Console                                             |
| `MODEL_PATH`           | `./model/rice_leaf_model.keras`                                     |
| `FLASK_DEBUG`          | `false`                                                             |

---

## 🔐 LANGKAH 9 — Setup Google OAuth

1. Buka https://console.cloud.google.com → pilih/buat project
2. **APIs & Services** → **Credentials** → **+ Create Credentials** → **OAuth 2.0 Client ID**
3. Setup consent screen jika diminta (External, isi nama app)
4. Application type: **Web application**
5. Authorized redirect URIs — tambahkan keduanya:
   ```
   http://localhost:8000/auth/google/callback
   https://ricescanai.onrender.com/auth/google/callback
   ```

   > ⚠️ Ganti `ricescanai` dengan nama app kamu yang sebenarnya di Render!
   >
6. Copy **Client ID** dan **Client Secret** → isi di Render environment vars

---

## ▶️ LANGKAH 10 — Deploy!

1. Render Web Service → **Manual Deploy** → **Deploy latest commit**
2. Pantau tab **Logs** — tunggu sampai muncul:
   ```
   ✅ Database siap
   Listening at: http://0.0.0.0:10000
   ```
3. Buka URL app kamu!

**Login admin default:**

- Username: `admin`
- Password: `admin123`
- ⚠️ Ganti password segera setelah login pertama!

---

## 🆘 TROUBLESHOOTING

| Error                                  | Solusi                                                                         |
| -------------------------------------- | ------------------------------------------------------------------------------ |
| `No module named 'dotenv'`           | Tambahkan `python-dotenv` ke `requirements.txt`                            |
| `Failed to load model` / `OSError` | Cek `MODEL_PATH` env var + pastikan folder `model/` ada di GitHub          |
| `Model file > 100MB gagal push`      | Pakai Git LFS (lihat Langkah 3)                                                |
| `postgres://` error                  | `DATABASE_URL` harus pakai `postgresql://` — app.py sudah auto-fix ini ✅ |
| `redirect_uri_mismatch` OAuth        | Tambahkan URL Render di Google Cloud Console                                   |
| `WORKER TIMEOUT`                     | Ganti `--timeout 120` jadi `--timeout 180` di Start Command                |
| `ricescan.db` masih muncul di GitHub | Jalankan `git rm --cached ricescan.db` lalu push                             |
