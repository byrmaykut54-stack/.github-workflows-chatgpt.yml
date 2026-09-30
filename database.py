import os
from datetime import datetime, timezone
from contextlib import contextmanager

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    plan TEXT NOT NULL DEFAULT 'trial',
    status TEXT NOT NULL DEFAULT 'active',
    trial_ends_at TIMESTAMPTZ,
    whatsapp_phone_number_id TEXT NOT NULL DEFAULT '',
    whatsapp_access_token TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customers (
    id BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (business_id, phone)
);

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    customer_id BIGINT REFERENCES customers(id) ON DELETE SET NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS appointments_business_date_idx
ON appointments (business_id, date, time);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    message_id TEXT,
    channel TEXT NOT NULL,
    direction TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    intent TEXT NOT NULL DEFAULT 'other',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_business_created_idx
ON messages (business_id, created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',
    permissions JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    email_verified_at TIMESTAMPTZ,
    UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT '',
    provider_customer_id TEXT NOT NULL DEFAULT '',
    provider_subscription_id TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT 'pro',
    status TEXT NOT NULL DEFAULT 'inactive',
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (business_id)
);

CREATE INDEX IF NOT EXISTS subscriptions_status_idx ON subscriptions (status);

CREATE TABLE IF NOT EXISTS billing_checkout_sessions (
    id BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    checkout_token TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS billing_checkout_business_idx ON billing_checkout_sessions (business_id);

CREATE TABLE IF NOT EXISTS sessions (
    id BIGSERIAL PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions (expires_at);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
 token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS email_verification_tokens (
 token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rate_limits (
 key TEXT PRIMARY KEY,
 window_started_at TIMESTAMPTZ NOT NULL,
 hits INTEGER NOT NULL DEFAULT 0,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS appointment_reminders (
    appointment_id TEXT NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (appointment_id, channel)
);

CREATE TABLE IF NOT EXISTS google_calendar_connections (business_id BIGINT PRIMARY KEY REFERENCES businesses(id) ON DELETE CASCADE,user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,calendar_id TEXT NOT NULL DEFAULT 'primary',calendar_name TEXT NOT NULL DEFAULT '',access_token TEXT NOT NULL DEFAULT '',refresh_token TEXT NOT NULL DEFAULT '',access_token_expires_at TIMESTAMPTZ,scopes TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS google_oauth_states (state TEXT PRIMARY KEY,business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS google_calendar_events (business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,appointment_id TEXT NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,event_id TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),PRIMARY KEY (business_id,appointment_id),UNIQUE (business_id,event_id));
CREATE TABLE IF NOT EXISTS usage_counters (
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    period_start DATE NOT NULL,
    metric TEXT NOT NULL,
    used_count BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (business_id, period_start, metric)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL DEFAULT '',
    target_id TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS audit_logs_business_created_idx
ON audit_logs (business_id, created_at DESC);
"""

@contextmanager
def connection():
    if not DATABASE_URL or psycopg is None:
        raise RuntimeError("DATABASE_URL veya psycopg kullanılamıyor.")
    with psycopg.connect(DATABASE_URL) as conn:
        yield conn

def enabled():
    return bool(DATABASE_URL and psycopg is not None)

def ensure_schema():
    if not enabled():
        return
    with connection() as conn:
        conn.execute(SCHEMA)
        conn.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'trial'")
        conn.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
        conn.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS whatsapp_phone_number_id TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE businesses ADD COLUMN IF NOT EXISTS whatsapp_access_token TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '{}'::jsonb")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE sessions ALTER COLUMN expires_at DROP NOT NULL")
        conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ")
        conn.commit()

def delete_users_by_email_prefix(prefix):
    """Delete accounts and their businesses for a one-time administrative cleanup."""
    prefix = str(prefix or "").strip().lower()
    if not prefix:
        return 0
    with connection() as conn:
        rows = conn.execute("SELECT id, business_id FROM users WHERE lower(email) LIKE %s", (prefix + "%",)).fetchall()
        business_ids = sorted({row[1] for row in rows})
        deleted = 0
        for business_id in business_ids:
            other_users = conn.execute("SELECT COUNT(*) FROM users WHERE business_id=%s AND lower(email) NOT LIKE %s", (business_id, prefix + "%")).fetchone()[0]
            if int(other_users) == 0:
                conn.execute("DELETE FROM businesses WHERE id=%s", (business_id,))
            else:
                conn.execute("DELETE FROM users WHERE business_id=%s AND lower(email) LIKE %s", (business_id, prefix + "%"))
            deleted += sum(1 for row in rows if row[1] == business_id)
        conn.execute("DELETE FROM rate_limits WHERE key LIKE 'register:%'")
        conn.commit()
        return deleted

def _fernet():
    key = os.environ.get("NEXORA_ENCRYPTION_KEY", "").strip()
    if not key or Fernet is None:
        return None
    return Fernet(key.encode("utf-8"))

def encrypt_secret(value):
    value = str(value or "")
    if not value:
        return ""
    f = _fernet()
    if not f:
        return value
    return f.encrypt(value.encode("utf-8")).decode("utf-8")

def decrypt_secret(value):
    value = str(value or "")
    if not value:
        return ""
    f = _fernet()
    if not f or not value.startswith("gAAAA"):
        return value
    try:
        return f.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("NEXORA_ENCRYPTION_KEY ile WhatsApp erişim anahtarı çözülemedi.")

def shared_rate_limited(key, limit, window_seconds):
    now = datetime.now(timezone.utc)
    with connection() as conn:
        row = conn.execute(
            "SELECT window_started_at, hits FROM rate_limits WHERE key=%s FOR UPDATE",
            (key,)
        ).fetchone()
        if not row or (now - row[0]).total_seconds() >= window_seconds:
            conn.execute(
                "INSERT INTO rate_limits (key,window_started_at,hits,updated_at) VALUES (%s,%s,1,NOW()) "
                "ON CONFLICT (key) DO UPDATE SET window_started_at=EXCLUDED.window_started_at,hits=1,updated_at=NOW()",
                (key, now)
            )
            conn.commit()
            return False
        hits = int(row[1]) + 1
        conn.execute("UPDATE rate_limits SET hits=%s,updated_at=NOW() WHERE key=%s", (hits, key))
        conn.commit()
        return hits > limit

def create_business(name, config):
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO businesses (name, config, plan, status, trial_ends_at) VALUES (%s, %s, 'trial', 'active', NOW() + INTERVAL '14 days') RETURNING id",
            (name, psycopg.types.json.Json(config))
        ).fetchone()
        conn.commit()
        return row[0]

def get_business_id(config):
    ensure_schema()
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM businesses WHERE name = %s ORDER BY id LIMIT 1",
            (config.get("business_name", "Demo İşletme"),)
        ).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "INSERT INTO businesses (name, config) VALUES (%s, %s) RETURNING id",
            (config.get("business_name", "Demo İşletme"), psycopg.types.json.Json(config))
        ).fetchone()
        conn.commit()
        return row[0]

def get_business_for_user(user_id):
    with connection() as conn:
        return conn.execute("SELECT b.id,b.name,b.config FROM users u JOIN businesses b ON b.id=u.business_id WHERE u.id=%s", (user_id,)).fetchone()

def get_user_by_email(email):
    with connection() as conn:
        return conn.execute("SELECT id, business_id, email, password_hash, role FROM users WHERE lower(email)=lower(%s) LIMIT 1", (email.strip(),)).fetchone()

def count_users():
    with connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

def create_user(email, password_hash, business_id, role="owner"):
    with connection() as conn:
        row = conn.execute("INSERT INTO users (business_id,email,password_hash,role,permissions) VALUES (%s,%s,%s,%s,%s) RETURNING id", (business_id, email.strip().lower(), password_hash, role, psycopg.types.json.Json({"appointments":True,"customers":True,"messages":True,"business_settings":role=="owner","team":role=="owner","reports":True}))).fetchone()
        conn.commit()
        return row[0]

def create_password_reset_token(token_hash, user_id, expires_at):
    with connection() as conn:
        conn.execute("DELETE FROM password_reset_tokens WHERE user_id=%s OR expires_at<NOW()", (user_id,))
        conn.execute("INSERT INTO password_reset_tokens (token_hash,user_id,expires_at) VALUES (%s,%s,%s)", (token_hash,user_id,expires_at))
        conn.commit()

def consume_password_reset_token(token_hash):
    with connection() as conn:
        row=conn.execute("SELECT user_id FROM password_reset_tokens WHERE token_hash=%s AND used_at IS NULL AND expires_at>NOW()", (token_hash,)).fetchone()
        if not row: return None
        conn.execute("UPDATE password_reset_tokens SET used_at=NOW() WHERE token_hash=%s", (token_hash,))
        conn.commit()
        return row[0]

def set_user_password(user_id, password_hash):
    with connection() as conn:
        conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hash,user_id))
        conn.execute("UPDATE sessions SET revoked_at=NOW() WHERE user_id=%s AND revoked_at IS NULL", (user_id,))
        conn.commit()

def create_email_verification_token(token_hash, user_id, expires_at):
    with connection() as conn:
        conn.execute("DELETE FROM email_verification_tokens WHERE user_id=%s OR expires_at<NOW()", (user_id,))
        conn.execute("INSERT INTO email_verification_tokens (token_hash,user_id,expires_at) VALUES (%s,%s,%s)", (token_hash,user_id,expires_at))
        conn.commit()

def consume_email_verification_token(token_hash):
    with connection() as conn:
        row=conn.execute("SELECT user_id FROM email_verification_tokens WHERE token_hash=%s AND used_at IS NULL AND expires_at>NOW()", (token_hash,)).fetchone()
        if not row: return None
        conn.execute("UPDATE email_verification_tokens SET used_at=NOW() WHERE token_hash=%s", (token_hash,))
        conn.commit()
        return row[0]

def mark_email_verified(user_id):
    with connection() as conn:
        conn.execute("UPDATE users SET email_verified_at=NOW() WHERE id=%s", (user_id,))
        conn.commit()

def create_session(token_hash, user_id, expires_at):
    with connection() as conn:
        conn.execute("INSERT INTO sessions (token_hash,user_id,expires_at) VALUES (%s,%s,%s)", (token_hash, user_id, expires_at))
        conn.commit()

def get_session_user(token_hash):
    with connection() as conn:
        return conn.execute("SELECT u.id,u.business_id,u.email,u.role,b.name,b.config FROM sessions s JOIN users u ON u.id=s.user_id JOIN businesses b ON b.id=u.business_id WHERE s.token_hash=%s AND s.revoked_at IS NULL AND (s.expires_at IS NULL OR s.expires_at>NOW()) LIMIT 1", (token_hash,)).fetchone()

def revoke_session(token_hash):
    with connection() as conn:
        conn.execute("UPDATE sessions SET revoked_at=NOW() WHERE token_hash=%s", (token_hash,))
        conn.commit()

def delete_session(token_hash):
    with connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=%s", (token_hash,))
        conn.commit()
def load_appointments(config):
    business_id = get_business_id(config)
    with connection() as conn:
        rows = conn.execute("""
            SELECT id, customer_name, phone, date, time, service, note, status,
                   to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            FROM appointments
            WHERE business_id = %s
            ORDER BY created_at
        """, (business_id,)).fetchall()
    keys = ["id","customer_name","phone","date","time","service","note","status","created_at"]
    return [dict(zip(keys, row)) for row in rows]

def save_appointments(config, items):
    business_id = get_business_id(config)
    with connection() as conn:
        for item in items:
            conn.execute("""
                INSERT INTO customers (business_id, name, phone)
                VALUES (%s, %s, %s)
                ON CONFLICT (business_id, phone)
                DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()
            """, (business_id, item.get("customer_name",""), item.get("phone","")))
            customer = conn.execute(
                "SELECT id FROM customers WHERE business_id=%s AND phone=%s",
                (business_id, item.get("phone",""))
            ).fetchone()
            conn.execute("""
                INSERT INTO appointments
                (id,business_id,customer_id,customer_name,phone,date,time,service,note,status,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz,NOW()))
                ON CONFLICT (id) DO UPDATE SET
                  customer_id=EXCLUDED.customer_id, customer_name=EXCLUDED.customer_name,
                  phone=EXCLUDED.phone, date=EXCLUDED.date, time=EXCLUDED.time,
                  service=EXCLUDED.service, note=EXCLUDED.note, status=EXCLUDED.status
            """, (
                item["id"], business_id, customer[0] if customer else None,
                item.get("customer_name",""), item.get("phone",""), item.get("date",""),
                item.get("time",""), item.get("service",""), item.get("note",""),
                item.get("status","pending"), item.get("created_at")
            ))
        conn.commit()

def load_messages(config):
    business_id = get_business_id(config)
    with connection() as conn:
        rows = conn.execute("""
            SELECT id, channel, direction, customer_name, phone, message, intent,
                   to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                   COALESCE(message_id,'')
            FROM messages WHERE business_id=%s ORDER BY created_at
        """, (business_id,)).fetchall()
    keys = ["id","channel","direction","customer_name","phone","message","intent","created_at","message_id"]
    return [dict(zip(keys,row)) for row in rows]

def save_messages(config, items):
    business_id = get_business_id(config)
    with connection() as conn:
        for item in items:
            conn.execute("""
                INSERT INTO messages
                (id,business_id,message_id,channel,direction,customer_name,phone,message,intent,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz,NOW()))
                ON CONFLICT (id) DO UPDATE SET
                  message_id=EXCLUDED.message_id, channel=EXCLUDED.channel,
                  direction=EXCLUDED.direction, customer_name=EXCLUDED.customer_name,
                  phone=EXCLUDED.phone, message=EXCLUDED.message, intent=EXCLUDED.intent
            """, (
                item["id"], business_id, item.get("message_id",""), item.get("channel","web"),
                item.get("direction","inbound"), item.get("customer_name",""), item.get("phone",""),
                item.get("message",""), item.get("intent","other"), item.get("created_at")
            ))
        conn.commit()


def list_due_appointments():
    with connection() as conn:
        return conn.execute("""
            SELECT a.id,a.business_id,a.customer_name,a.phone,a.date,a.time,a.service,a.note,
                   b.name,b.config
            FROM appointments a
            JOIN businesses b ON b.id=a.business_id
            WHERE a.status IN ('pending','confirmed')
              AND NOT EXISTS (
                  SELECT 1 FROM appointment_reminders r
                  WHERE r.appointment_id=a.id
              )
        """).fetchall()

def reminder_sent(appointment_id, channel):
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM appointment_reminders WHERE appointment_id=%s AND channel=%s",
            (appointment_id, channel)
        ).fetchone()
    return bool(row)

def mark_reminder_sent(appointment_id, channel):
    with connection() as conn:
        conn.execute(
            "INSERT INTO appointment_reminders (appointment_id,channel) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (appointment_id, channel)
        )
        conn.commit()

def get_business_config(business_id):
    with connection() as conn:
        row = conn.execute("SELECT config FROM businesses WHERE id=%s", (business_id,)).fetchone()
        return row[0] if row else {}

def get_business_whatsapp(business_id):
    with connection() as conn:
        row = conn.execute(
            "SELECT whatsapp_phone_number_id, whatsapp_access_token FROM businesses WHERE id=%s",
            (business_id,)
        ).fetchone()
    if not row:
        return {"phone_number_id":"", "access_token":""}
    token = decrypt_secret(row[1])
    if token and row[1] == token and _fernet():
        encrypted = encrypt_secret(token)
        with connection() as conn:
            conn.execute("UPDATE businesses SET whatsapp_access_token=%s WHERE id=%s", (encrypted, business_id))
            conn.commit()
    return {"phone_number_id": row[0], "access_token": token}

def update_business_whatsapp(business_id, phone_number_id=None, access_token=None):
    current = get_business_whatsapp(business_id)
    phone_number_id = current["phone_number_id"] if phone_number_id is None else str(phone_number_id).strip()
    access_token = current["access_token"] if access_token is None else str(access_token).strip()
    stored_token = encrypt_secret(access_token)
    with connection() as conn:
        conn.execute(
            "UPDATE businesses SET whatsapp_phone_number_id=%s, whatsapp_access_token=%s WHERE id=%s",
            (phone_number_id, stored_token, business_id)
        )
        conn.commit()

def find_business_by_whatsapp_phone_number_id(phone_number_id):
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM businesses WHERE whatsapp_phone_number_id=%s LIMIT 1",
            (str(phone_number_id).strip(),)
        ).fetchone()
        return row[0] if row else None

def update_business(business_id, name, config):
    with connection() as conn:
        conn.execute("UPDATE businesses SET name=%s, config=%s WHERE id=%s", (name, psycopg.types.json.Json(config), business_id))
        conn.commit()

def load_appointments_by_business(business_id):
    with connection() as conn:
        rows = conn.execute("""SELECT id,customer_name,phone,date,time,service,note,status,to_char(created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM appointments WHERE business_id=%s ORDER BY created_at""",(business_id,)).fetchall()
    keys=["id","customer_name","phone","date","time","service","note","status","created_at"]
    return [dict(zip(keys,r)) for r in rows]

def save_appointments_by_business(business_id, items):
    with connection() as conn:
        for item in items:
            conn.execute("""INSERT INTO customers (business_id,name,phone) VALUES (%s,%s,%s)
                ON CONFLICT (business_id,phone) DO UPDATE SET name=EXCLUDED.name,updated_at=NOW()""",
                (business_id,item.get("customer_name",""),item.get("phone","")))
            customer=conn.execute("SELECT id FROM customers WHERE business_id=%s AND phone=%s",(business_id,item.get("phone",""))).fetchone()
            conn.execute("""INSERT INTO appointments (id,business_id,customer_id,customer_name,phone,date,time,service,note,status,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz,NOW()))
                ON CONFLICT (id) DO UPDATE SET customer_id=EXCLUDED.customer_id,customer_name=EXCLUDED.customer_name,phone=EXCLUDED.phone,date=EXCLUDED.date,time=EXCLUDED.time,service=EXCLUDED.service,note=EXCLUDED.note,status=EXCLUDED.status""",
                (item["id"],business_id,customer[0] if customer else None,item.get("customer_name",""),item.get("phone",""),item.get("date",""),item.get("time",""),item.get("service",""),item.get("note",""),item.get("status","pending"),item.get("created_at")))
        conn.commit()

def load_messages_by_business(business_id):
    with connection() as conn:
        rows=conn.execute("""SELECT id,channel,direction,customer_name,phone,message,intent,to_char(created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'),COALESCE(message_id,'') FROM messages WHERE business_id=%s ORDER BY created_at""",(business_id,)).fetchall()
    keys=["id","channel","direction","customer_name","phone","message","intent","created_at","message_id"]
    return [dict(zip(keys,r)) for r in rows]

def save_messages_by_business(business_id, items):
    with connection() as conn:
        for item in items:
            conn.execute("""INSERT INTO messages (id,business_id,message_id,channel,direction,customer_name,phone,message,intent,created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz,NOW()))
                ON CONFLICT (id) DO UPDATE SET message_id=EXCLUDED.message_id,channel=EXCLUDED.channel,direction=EXCLUDED.direction,customer_name=EXCLUDED.customer_name,phone=EXCLUDED.phone,message=EXCLUDED.message,intent=EXCLUDED.intent""",
                (item["id"],business_id,item.get("message_id",""),item.get("channel","web"),item.get("direction","inbound"),item.get("customer_name",""),item.get("phone",""),item.get("message",""),item.get("intent","other"),item.get("created_at")))
        conn.commit()


def list_customers_by_business(business_id):
    with connection() as conn:
        return conn.execute(
            "SELECT id,name,phone,created_at,updated_at FROM customers WHERE business_id=%s ORDER BY updated_at DESC",
            (business_id,)
        ).fetchall()

def get_business_report(business_id):
    with connection() as conn:
        appointments = conn.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE status='pending'), COUNT(*) FILTER (WHERE status='confirmed'), COUNT(*) FILTER (WHERE status='completed'), COUNT(*) FILTER (WHERE status='cancelled') FROM appointments WHERE business_id=%s",
            (business_id,)
        ).fetchone()
        customers = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE business_id=%s",
            (business_id,)
        ).fetchone()[0]
        messages = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE business_id=%s",
            (business_id,)
        ).fetchone()[0]
        return {
            "appointments_total": appointments[0],
            "appointments_pending": appointments[1],
            "appointments_confirmed": appointments[2],
            "appointments_completed": appointments[3],
            "appointments_cancelled": appointments[4],
            "customers_total": customers,
            "messages_total": messages
        }

def list_users(business_id):
    with connection() as conn:
        return conn.execute("SELECT id,email,role,created_at FROM users WHERE business_id=%s ORDER BY created_at",(business_id,)).fetchall()

def get_user_in_business(business_id, user_id):
    with connection() as conn:
        return conn.execute(
            "SELECT id,email,role FROM users WHERE id=%s AND business_id=%s",
            (user_id, business_id)
        ).fetchone()

def get_user_permissions(business_id, user_id):
    with connection() as conn:
        row=conn.execute("SELECT permissions FROM users WHERE id=%s AND business_id=%s",(user_id,business_id)).fetchone()
        return row[0] if row and row[0] else {}

def count_owners(business_id):
    with connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE business_id=%s AND role='owner'",
            (business_id,)
        ).fetchone()[0]

def update_user_role(business_id,user_id,role):
    with connection() as conn:
        conn.execute("UPDATE users SET role=%s WHERE id=%s AND business_id=%s",(role,user_id,business_id))
        conn.commit()


def delete_account(user_id, business_id):
    """Permanently delete the signed-in owner's account and entire business workspace atomically."""
    with connection() as conn:
        row=conn.execute(
            "SELECT id,email,role FROM users WHERE id=%s AND business_id=%s FOR UPDATE",
            (user_id,business_id)
        ).fetchone()
        if not row:
            return None
        if row[2] != "owner":
            raise PermissionError("Sadece işletme sahibi hesabını silebilir.")
        # businesses is the root record; dependent records use ON DELETE CASCADE.
        conn.execute("DELETE FROM businesses WHERE id=%s", (business_id,))
        conn.commit()
        return {"id":row[0],"email":row[1]}

def delete_user(business_id, user_id):
    with connection() as conn:
        row=conn.execute("DELETE FROM users WHERE id=%s AND business_id=%s RETURNING id",(user_id,business_id)).fetchone()
        conn.commit()
        return bool(row)

def write_audit_log(business_id, user_id, action, target_type="", target_id="", details=None):
    with connection() as conn:
        conn.execute("INSERT INTO audit_logs (business_id,user_id,action,target_type,target_id,details) VALUES (%s,%s,%s,%s,%s,%s)",
                     (business_id,user_id,action,target_type,str(target_id),psycopg.types.json.Json(details or {})))
        conn.commit()

def list_audit_logs(business_id, limit=100):
    with connection() as conn:
        return conn.execute("SELECT id,user_id,action,target_type,target_id,details,created_at FROM audit_logs WHERE business_id=%s ORDER BY created_at DESC LIMIT %s",(business_id,min(max(int(limit),1),200))).fetchall()

def get_user_permissions(user_id):
    with connection() as conn:
        row=conn.execute("SELECT role, permissions FROM users WHERE id=%s",(user_id,)).fetchone()
        if not row: return None
        if row[0]=='owner': return {'appointments':True,'customers':True,'messages':True,'business_settings':True,'team':True,'reports':True}
        return row[1] or {}

def update_user_permissions(business_id,user_id,permissions):
    with connection() as conn:
        row=conn.execute("UPDATE users SET permissions=%s WHERE id=%s AND business_id=%s RETURNING id",(psycopg.types.json.Json(permissions),user_id,business_id)).fetchone()
        conn.commit()
        return bool(row)
def create_google_oauth_state(state,business_id,user_id,expires_at):
    with connection() as conn:
        conn.execute("DELETE FROM google_oauth_states WHERE expires_at<NOW()")
        conn.execute("INSERT INTO google_oauth_states (state,business_id,user_id,expires_at) VALUES (%s,%s,%s,%s)",(state,business_id,user_id,expires_at)); conn.commit()

def consume_google_oauth_state(state):
    with connection() as conn:
        row=conn.execute("DELETE FROM google_oauth_states WHERE state=%s AND expires_at>NOW() RETURNING business_id,user_id",(state,)).fetchone(); conn.commit()
    return {"business_id":row[0],"user_id":row[1]} if row else None

def save_google_calendar_connection(business_id,user_id,calendar_id,calendar_name,access_token,refresh_token,expires_at,scopes):
    with connection() as conn:
        conn.execute("""INSERT INTO google_calendar_connections (business_id,user_id,calendar_id,calendar_name,access_token,refresh_token,access_token_expires_at,scopes) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (business_id) DO UPDATE SET user_id=EXCLUDED.user_id,calendar_id=EXCLUDED.calendar_id,calendar_name=EXCLUDED.calendar_name,access_token=EXCLUDED.access_token,refresh_token=EXCLUDED.refresh_token,access_token_expires_at=EXCLUDED.access_token_expires_at,scopes=EXCLUDED.scopes,updated_at=NOW()""",(business_id,user_id,calendar_id,calendar_name,encrypt_secret(access_token),encrypt_secret(refresh_token),expires_at,scopes)); conn.commit()

def get_google_calendar_connection(business_id):
    with connection() as conn:
        r=conn.execute("SELECT calendar_id,calendar_name,access_token,refresh_token,access_token_expires_at,scopes FROM google_calendar_connections WHERE business_id=%s",(business_id,)).fetchone()
    if not r:return None
    return {"calendar_id":r[0],"calendar_name":r[1],"access_token":decrypt_secret(r[2]),"refresh_token":decrypt_secret(r[3]),"access_token_expires_at":r[4].isoformat() if r[4] else None,"scopes":r[5]}

def update_google_calendar_tokens(business_id,access_token,expires_at):
    with connection() as conn:
        conn.execute("UPDATE google_calendar_connections SET access_token=%s,access_token_expires_at=%s,updated_at=NOW() WHERE business_id=%s",(encrypt_secret(access_token),expires_at,business_id)); conn.commit()

def delete_google_calendar_connection(business_id):
    with connection() as conn:
        conn.execute("DELETE FROM google_calendar_connections WHERE business_id=%s",(business_id,)); conn.commit()

def save_google_event(appointment_id,business_id,event_id):
    with connection() as conn:
        conn.execute("INSERT INTO google_calendar_events (business_id,appointment_id,event_id) VALUES (%s,%s,%s) ON CONFLICT (business_id,appointment_id) DO UPDATE SET event_id=EXCLUDED.event_id,updated_at=NOW()",(business_id,appointment_id,event_id)); conn.commit()

def get_google_event_id(appointment_id,business_id):
    with connection() as conn:
        r=conn.execute("SELECT event_id FROM google_calendar_events WHERE appointment_id=%s AND business_id=%s",(appointment_id,business_id)).fetchone()
    return r[0] if r else None

def delete_google_event(appointment_id,business_id):
    with connection() as conn:
        conn.execute("DELETE FROM google_calendar_events WHERE appointment_id=%s AND business_id=%s",(appointment_id,business_id,)); conn.commit()

def get_usage(business_id, metric, period_start=None):
    period_start = period_start or datetime.now(timezone.utc).date().replace(day=1)
    with connection() as conn:
        row=conn.execute(
            "SELECT used_count FROM usage_counters WHERE business_id=%s AND period_start=%s AND metric=%s",
            (business_id,period_start,metric)
        ).fetchone()
    return int(row[0]) if row else 0

def increment_usage(business_id, metric, amount=1):
    period_start=datetime.now(timezone.utc).date().replace(day=1)
    with connection() as conn:
        row=conn.execute(
            "INSERT INTO usage_counters (business_id,period_start,metric,used_count) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (business_id,period_start,metric) DO UPDATE SET used_count=usage_counters.used_count+EXCLUDED.used_count,updated_at=NOW() "
            "RETURNING used_count",
            (business_id,period_start,metric,amount)
        ).fetchone()
        conn.commit()
    return int(row[0])

def get_usage_summary(business_id):
    return {
        "appointments": get_usage(business_id,"appointments"),
        "ai_messages": get_usage(business_id,"ai_messages")
    }

def get_business_subscription(business_id):
    with connection() as conn:
        row = conn.execute(
            "SELECT plan,status,trial_ends_at FROM businesses WHERE id=%s",
            (business_id,)
        ).fetchone()
    if not row:
        return {"plan":"trial","status":"unknown","trial_ends_at":None,"trial_active":False}
    trial_ends = row[2]
    active = row[1] == "active" and (row[0] != "trial" or trial_ends is None or trial_ends > datetime.now(timezone.utc))
    return {"plan":row[0],"status":row[1],"trial_ends_at":trial_ends.isoformat() if trial_ends else None,"trial_active":active}

def get_subscription_record(business_id):
    with connection() as conn:
        row = conn.execute(
            "SELECT id,provider,provider_customer_id,provider_subscription_id,plan,status,current_period_start,current_period_end,cancel_at_period_end FROM subscriptions WHERE business_id=%s",
            (business_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "provider": row[1], "provider_customer_id": row[2],
        "provider_subscription_id": row[3], "plan": row[4], "status": row[5],
        "current_period_start": row[6].isoformat() if row[6] else None,
        "current_period_end": row[7].isoformat() if row[7] else None,
        "cancel_at_period_end": bool(row[8])
    }

def create_billing_checkout_session(business_id, provider, checkout_token, conversation_id=""):
    with connection() as conn:
        row=conn.execute(
            "INSERT INTO billing_checkout_sessions (business_id,provider,checkout_token,conversation_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (business_id,str(provider or ""),str(checkout_token or ""),str(conversation_id or ""))
        ).fetchone()
        conn.commit()
        return row[0]

def get_billing_checkout_session(checkout_token):
    with connection() as conn:
        return conn.execute(
            "SELECT id,business_id,provider,checkout_token,conversation_id,created_at,completed_at FROM billing_checkout_sessions WHERE checkout_token=%s LIMIT 1",
            (str(checkout_token or ""),)
        ).fetchone()

def complete_billing_checkout(checkout_token):
    with connection() as conn:
        conn.execute("UPDATE billing_checkout_sessions SET completed_at=NOW() WHERE checkout_token=%s AND completed_at IS NULL",(str(checkout_token or ""),))
        conn.commit()

def _parse_subscription_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z","+00:00"))
    except (TypeError, ValueError):
        return None

def create_or_update_subscription(business_id, provider, provider_customer_id, provider_subscription_id, status="pending", period_start=None, period_end=None, cancel_at_period_end=False):
    status=str(status or "inactive").strip().lower()
    plan="pro"
    start=_parse_subscription_time(period_start)
    end=_parse_subscription_time(period_end)
    with connection() as conn:
        row=conn.execute("""
            INSERT INTO subscriptions
              (business_id,provider,provider_customer_id,provider_subscription_id,plan,status,current_period_start,current_period_end,cancel_at_period_end)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (business_id) DO UPDATE SET
              provider=EXCLUDED.provider,
              provider_customer_id=EXCLUDED.provider_customer_id,
              provider_subscription_id=EXCLUDED.provider_subscription_id,
              plan=EXCLUDED.plan,
              status=EXCLUDED.status,
              current_period_start=COALESCE(EXCLUDED.current_period_start,subscriptions.current_period_start),
              current_period_end=COALESCE(EXCLUDED.current_period_end,subscriptions.current_period_end),
              cancel_at_period_end=EXCLUDED.cancel_at_period_end,
              updated_at=NOW()
            RETURNING id
        """,(business_id,str(provider or ""),str(provider_customer_id or ""),str(provider_subscription_id or ""),plan,status,start,end,bool(cancel_at_period_end))).fetchone()
        business_status="active" if status in {"active","trialing"} else "inactive"
        business_plan="pro" if status in {"active","trialing","past_due","unpaid"} else "trial"
        conn.execute("UPDATE businesses SET plan=%s,status=%s WHERE id=%s",(business_plan,business_status,business_id))
        conn.commit()
        return row[0]

def update_subscription_status(business_id,status,cancel_at_period_end=None,period_end=None):
    status=str(status or "inactive").strip().lower()
    with connection() as conn:
        if cancel_at_period_end is None and period_end is None:
            conn.execute("UPDATE subscriptions SET status=%s,updated_at=NOW() WHERE business_id=%s",(status,business_id))
        else:
            end=_parse_subscription_time(period_end)
            conn.execute("UPDATE subscriptions SET status=%s,cancel_at_period_end=COALESCE(%s,cancel_at_period_end),current_period_end=COALESCE(%s,current_period_end),updated_at=NOW() WHERE business_id=%s",(status,cancel_at_period_end,end,business_id))
        business_status="active" if status in {"active","trialing"} else "inactive"
        business_plan="pro" if status in {"active","trialing","past_due","unpaid"} else "trial"
        conn.execute("UPDATE businesses SET plan=%s,status=%s WHERE id=%s",(business_plan,business_status,business_id))
        conn.commit()

def get_business_by_provider_subscription(provider_subscription_id):
    with connection() as conn:
        row=conn.execute("SELECT business_id FROM subscriptions WHERE provider_subscription_id=%s LIMIT 1",(str(provider_subscription_id or ""),)).fetchone()
    return row[0] if row else None
