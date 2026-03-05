import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'ricescan.db')

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()

print("Memulai migrasi...")

# Buat tabel users baru dengan password_hash nullable
cur.executescript("""
    PRAGMA foreign_keys = OFF;

    CREATE TABLE IF NOT EXISTS users_new (
        id            INTEGER PRIMARY KEY,
        nama          VARCHAR(100) NOT NULL,
        username      VARCHAR(50)  UNIQUE NOT NULL,
        email         VARCHAR(120) UNIQUE NOT NULL,
        password_hash VARCHAR(256),
        role          VARCHAR(20)  NOT NULL DEFAULT 'user',
        foto_profil   VARCHAR(120),
        google_id     VARCHAR(120) UNIQUE,
        is_active     BOOLEAN      NOT NULL DEFAULT 1,
        created_at    DATETIME
    );

    INSERT INTO users_new
        (id, nama, username, email, password_hash, role, foto_profil, google_id, is_active, created_at)
    SELECT
        id, nama, username, email, password_hash, role, foto_profil,
        NULL,
        is_active, created_at
    FROM users;

    DROP TABLE users;

    ALTER TABLE users_new RENAME TO users;

    PRAGMA foreign_keys = ON;
""")

conn.commit()
conn.close()
print("✅ Migrasi selesai! password_hash sekarang nullable.")