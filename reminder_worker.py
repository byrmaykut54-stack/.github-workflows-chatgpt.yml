import json
import os
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import database

REMINDER_MINUTES = int(os.environ.get("NEXORA_REMINDER_MINUTES", "60") or 60)
WINDOW_MINUTES = int(os.environ.get("NEXORA_REMINDER_WINDOW", "5") or 5)

def parse_local_datetime(date_text, time_text, tz_name):
    try:
        tz = ZoneInfo(tz_name)
        return datetime.strptime(f"{date_text} {time_text}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    except Exception:
        return None

def send_email(to_address, subject, body):
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", username).strip()
    if not host or not sender or not to_address:
        return False
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_address
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(msg)
    return True

def send_whatsapp(phone_number_id, access_token, to, message):
    if not phone_number_id or not access_token or not to:
        return False
    url = f"https://graph.facebook.com/v22.0/{phone_number_id}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    return bool(data.get("messages"))

def owner_email(business_id):
    with database.connection() as conn:
        row = conn.execute(
            "SELECT email FROM users WHERE business_id=%s AND role='owner' ORDER BY id LIMIT 1",
            (business_id,)
        ).fetchone()
    return row[0] if row else ""

def main():
    if not database.enabled():
        raise SystemExit("DATABASE_URL hazır değil.")
    database.ensure_schema()
    now = datetime.now(timezone.utc)
    sent = 0
    skipped = 0

    for row in database.list_due_appointments():
        appointment_id, business_id, customer_name, phone, date_text, time_text, service, note, business_name, raw_config = row
        config = raw_config if isinstance(raw_config, dict) else json.loads(raw_config or "{}")
        tz_name = str(config.get("timezone", "Europe/Istanbul")).strip() or "Europe/Istanbul"
        local_dt = parse_local_datetime(str(date_text), str(time_text), tz_name)
        if not local_dt:
            skipped += 1
            continue

        due_at = local_dt.astimezone(timezone.utc) - timedelta(minutes=REMINDER_MINUTES)
        if not (due_at - timedelta(minutes=WINDOW_MINUTES) <= now <= due_at + timedelta(minutes=WINDOW_MINUTES)):
            continue

        message = (
            f"NEXORA hatırlatma: {business_name} işletmenizde {customer_name} adlı müşterinin "
            f"{date_text} {time_text} tarihli randevusu var. Hizmet: {service}."
        )
        whatsapp = database.get_business_whatsapp(business_id)
        delivered = False

        try:
            delivered = send_whatsapp(
                whatsapp.get("phone_number_id", ""),
                whatsapp.get("access_token", ""),
                phone,
                message
            )
            if delivered:
                database.mark_reminder_sent(appointment_id, "whatsapp")
        except Exception:
            delivered = False

        if not delivered:
            email = owner_email(business_id)
            try:
                delivered = send_email(
                    email,
                    f"NEXORA randevu hatırlatması — {date_text} {time_text}",
                    message + (f"\nNot: {note}" if note else "")
                )
                if delivered:
                    database.mark_reminder_sent(appointment_id, "email")
            except Exception:
                delivered = False

        if delivered:
            sent += 1

    print(f"NEXORA reminders: sent={sent} skipped={skipped}")

if __name__ == "__main__":
    main()
