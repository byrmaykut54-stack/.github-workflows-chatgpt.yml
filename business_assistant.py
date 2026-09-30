import hashlib
import hmac
import json
import os
import re
import urllib.request
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from collections import defaultdict

CONFIG_PATH = "business_config.json"
HTML_PATH = "index.html"
APPOINTMENTS_PATH = "appointments.json"
MESSAGES_PATH = "messages.json"

try:
    import database
except ImportError:
    database = None
try:
    import google_calendar
except ImportError:
    google_calendar = None

DEFAULT_CONFIG = {
    "business_name": "Demo İşletme",
    "sector": "Berber",
    "services": [
        {"name": "Saç Kesimi", "price": "500 TL"},
        {"name": "Saç + Sakal", "price": "750 TL"}
    ],
    "working_hours": "09:00-18:00",
    "closed_days": ["Pazar"],
    "address": "İşletme adresini config dosyasına yazın",
    "tone": "samimi, kısa ve profesyonel"
}

RATE_LIMITS = defaultdict(list)
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 20
REGISTER_RATE_LIMIT = 3
REGISTER_RATE_WINDOW = 3600
MAX_BODY_BYTES = 256 * 1024

SYSTEM_PROMPT = """Sen bir küçük işletme müşteri iletişim asistanısın.
İşletmenin verdiği bilgilere sadık kal. Bilgi yoksa uydurma.
Fiyat, çalışma saati veya randevu uygunluğu kesin değilse kesinmiş gibi söyleme.
Yanıtları Türkçe, kısa, sıcak ve profesyonel yaz.
Müşteri randevu istiyorsa tarih ve saat bilgisini netleştirmeye çalış.
Ödeme, sağlık, hukuki veya güvenlik konusunda işletmenin bilmediği bilgileri uydurma.
"""

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_config():
    return load_json(CONFIG_PATH, DEFAULT_CONFIG)

def load_config_for_user(user):
    if user and database_enabled():
        value = user[5]
        return json.loads(value) if isinstance(value, str) else value
    return load_config()

def database_enabled():
    return bool(database and database.enabled())

def hash_password(password):
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return "scrypt$16384$8$1$" + salt.hex() + "$" + derived.hex()

def verify_password(password, stored):
    try:
        _, n, r, pp, salt_hex, digest_hex = stored.split("$")
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(pp))
        return hmac.compare_digest(derived.hex(), digest_hex)
    except Exception:
        return False

def send_email(to_address, subject, body):
    """Send transactional mail via Resend when configured, otherwise SMTP."""
    resend_key=os.environ.get("RESEND_API_KEY","").strip()
    resend_from=os.environ.get("RESEND_FROM","").strip()
    if resend_key and resend_from:
        try:
            payload=json.dumps({"from":resend_from,"to":[to_address],"subject":subject,"text":body}).encode("utf-8")
            req=urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={"Authorization":"Bearer "+resend_key,"Content-Type":"application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req,timeout=20) as response:
                result=json.load(response)
            print("EMAIL_OK: Resend delivery request accepted", flush=True)
            return True
        except Exception as exc:
            print(f"EMAIL_ERROR: Resend {type(exc).__name__}: {exc}", flush=True)
            return False

    host=os.environ.get("SMTP_HOST","").strip()
    try:
        port=int(os.environ.get("SMTP_PORT","587") or 587)
    except ValueError:
        print("EMAIL_ERROR: SMTP_PORT geçersiz", flush=True)
        return False
    username=os.environ.get("SMTP_USERNAME","").strip()
    password=os.environ.get("SMTP_PASSWORD","")
    sender=os.environ.get("SMTP_FROM",username).strip()
    if not host or not sender:
        print("EMAIL_ERROR: RESEND_API_KEY/RESEND_FROM veya SMTP_HOST/SMTP_FROM eksik", flush=True)
        return False
    msg=EmailMessage()
    msg["From"]=sender
    msg["To"]=to_address
    msg["Subject"]=subject
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host,port,timeout=20) as smtp:
                if username:
                    smtp.login(username,password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host,port,timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if username:
                    smtp.login(username,password)
                smtp.send_message(msg)
        print("EMAIL_OK: SMTP delivery request accepted", flush=True)
        return True
    except Exception as exc:
        print(f"EMAIL_ERROR: SMTP {type(exc).__name__}: {exc}", flush=True)
        return False

def frontend_base_url():
    configured=os.environ.get("PUBLIC_APP_URL","").strip().rstrip("/")
    if configured:
        return configured
    host=os.environ.get("HOST","").strip()
    if host:
        return "https://"+host
    return "https://ai-isletme-asistani-1s10.onrender.com"

def make_one_time_token():
    token=os.urandom(32).hex()
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()

def cookie_token(handler):
    for part in handler.headers.get("Cookie", "").split(";"):
        if part.strip().startswith("session="): return part.strip().split("=", 1)[1]
    return ""

def current_user(handler):
    if not database_enabled(): return None
    token = cookie_token(handler)
    return database.get_session_user(hashlib.sha256(token.encode("utf-8")).hexdigest()) if token else None

def rate_limited(key, limit=RATE_LIMIT_MAX, window=RATE_LIMIT_WINDOW):
    if database_enabled():
        try:
            return database.shared_rate_limited(key, limit, window)
        except Exception:
            # Fail closed for abuse controls if the shared limiter is unavailable.
            return True
    now = datetime.now(timezone.utc).timestamp()
    values = [x for x in RATE_LIMITS[key] if now - x < window]
    if len(values) >= limit:
        RATE_LIMITS[key] = values
        return True
    values.append(now)
    RATE_LIMITS[key] = values
    return False

PLAN_LIMITS = {
    "trial": {"appointments": 50, "ai_messages": 200},
    "pro": {"appointments": 1000, "ai_messages": 5000},
}

def usage_limit_reached(user, metric):
    if not user or not database_enabled():
        return False
    sub=database.get_business_subscription(user[1])
    plan=str(sub.get("plan","trial")).lower()
    limit=PLAN_LIMITS.get(plan, PLAN_LIMITS["trial"]).get(metric)
    if limit is None:
        return False
    return database.get_usage(user[1],metric) >= limit

def usage_payload(business_id):
    sub=database.get_business_subscription(business_id)
    plan=str(sub.get("plan","trial")).lower()
    limits=PLAN_LIMITS.get(plan,PLAN_LIMITS["trial"])
    used=database.get_usage_summary(business_id)
    return {"plan":plan,"limits":limits,"used":used,"remaining":{k:max(v-used.get(k,0),0) for k,v in limits.items()}}

def master_admin_allowed(user):
    """NEXORA platform owner access is restricted to one configured email."""
    if not user:
        return False
    admin_email=os.environ.get("NEXORA_ADMIN_EMAIL","").strip().lower()
    return bool(admin_email) and str(user[2]).strip().lower()==admin_email and str(user[3]).strip().lower()=="owner"

def master_admin_required(handler):
    user=current_user(handler)
    if not user:
        handler.send_json(401,{"error":"Giriş yapmanız gerekiyor."})
        return None
    if not master_admin_allowed(user):
        handler.send_json(403,{"error":"NEXORA Master Panel yetkiniz yok."})
        return None
    return user

def master_stats():
    if not database_enabled():
        return {"businesses":0,"users":0,"appointments":0,"messages":0,"active_subscriptions":0}
    with database.connection() as conn:
        businesses=conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        users=conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        appointments=conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        messages=conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        active=conn.execute("SELECT COUNT(*) FROM businesses WHERE status='active'").fetchone()[0]
    return {"businesses":businesses,"users":users,"appointments":appointments,"messages":messages,"active_businesses":active}

def master_businesses():
    if not database_enabled(): return []
    with database.connection() as conn:
        rows=conn.execute("SELECT b.id,b.name,b.plan,b.status,b.trial_ends_at,b.created_at,(SELECT COUNT(*) FROM users u WHERE u.business_id=b.id),(SELECT COUNT(*) FROM appointments a WHERE a.business_id=b.id) FROM businesses b ORDER BY b.created_at DESC").fetchall()
    return [{"id":r[0],"name":r[1],"plan":r[2],"status":r[3],"trial_ends_at":r[4].isoformat() if r[4] else None,"created_at":r[5].isoformat() if r[5] else None,"users":r[6],"appointments":r[7]} for r in rows]

def auth_required(handler):
    user = current_user(handler)
    if not user:
        handler.send_json(401, {"error":"Giriş yapmanız gerekiyor."})
        return None
    if database_enabled():
        subscription = database.get_business_subscription(user[1])
        if subscription["status"] != "active":
            handler.send_json(403, {"error":"İşletme hesabı aktif değil."})
            return None
        if subscription["plan"] == "trial" and not subscription["trial_active"]:
            handler.send_json(402, {"error":"Deneme süreniz sona erdi. Devam etmek için Pro planına geçin."})
            return None
    return user

def session_required(handler):
    """Authenticate without blocking an expired trial so billing remains reachable."""
    user = current_user(handler)
    if not user:
        handler.send_json(401, {"error":"Giriş yapmanız gerekiyor."})
        return None
    if database_enabled():
        subscription = database.get_business_subscription(user[1])
        if subscription["status"] != "active":
            handler.send_json(403, {"error":"İşletme hesabı aktif değil."})
            return None
    return user


def subscription_payload(business_id):
    if not database_enabled():
        return {"plan":"trial","status":"unknown","trial_active":False,"subscription":None}
    base=database.get_business_subscription(business_id)
    base["subscription"]=database.get_subscription_record(business_id)
    return base

def verify_billing_signature(raw_body, signature):
    secret=os.environ.get("NEXORA_PAYMENT_WEBHOOK_SECRET","").strip()
    if not secret or not signature:
        return False
    expected="sha256="+hmac.new(secret.encode("utf-8"),raw_body,hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected,signature.strip())


def iyzico_authorization(uri_path, body_text):
    api_key=os.environ.get("IYZICO_API_KEY","").strip()
    secret_key=os.environ.get("IYZICO_SECRET_KEY","").strip()
    if not api_key or not secret_key:
        raise RuntimeError("IYZICO_API_KEY ve IYZICO_SECRET_KEY yapılandırılmalı.")
    random_key=str(int(datetime.now(timezone.utc).timestamp()*1000))+os.urandom(6).hex()
    signature=hmac.new(secret_key.encode("utf-8"),(random_key+uri_path+body_text).encode("utf-8"),hashlib.sha256).hexdigest()
    raw="apiKey:"+api_key+"&randomKey:"+random_key+"&signature:"+signature
    return "IYZWSv2 "+__import__("base64").b64encode(raw.encode("utf-8")).decode("ascii"),random_key

def iyzico_request(method, uri_path, payload=None):
    base=os.environ.get("IYZICO_BASE_URL","https://api.iyzipay.com").rstrip("/")
    body=json.dumps(payload or {},ensure_ascii=False,separators=(",",":"))
    auth,rnd=iyzico_authorization(uri_path,body)
    req=urllib.request.Request(base+uri_path,data=body.encode("utf-8") if method!="GET" else None,headers={"Authorization":auth,"x-iyzi-rnd":rnd,"Content-Type":"application/json"},method=method)
    with urllib.request.urlopen(req,timeout=30) as response:
        return json.load(response)

def iyzico_start_checkout(user):
    plan_ref=os.environ.get("IYZICO_PRICING_PLAN_REFERENCE_CODE","").strip()
    if not plan_ref:
        raise RuntimeError("IYZICO_PRICING_PLAN_REFERENCE_CODE yapılandırılmalı.")
    config=business_config_for_user(user)
    email=user[2]
    local=email.split("@",1)[0]
    name=config.get("business_name","NEXORA")
    callback=os.environ.get("IYZICO_SUBSCRIPTION_CALLBACK_URL","").strip()
    if not callback:
        raise RuntimeError("IYZICO_SUBSCRIPTION_CALLBACK_URL yapılandırılmalı.")
    payload={"locale":"tr","callbackUrl":callback,"pricingPlanReferenceCode":plan_ref,"subscriptionInitialStatus":"ACTIVE","conversationId":"nexora-"+str(user[1])+"-"+str(int(datetime.now(timezone.utc).timestamp())),"customer":{"name":name[:50],"surname":"Owner","email":email,"gsmNumber":config.get("phone",""),"billingAddress":{"address":config.get("address","NEXORA"),"zipCode":"","contactName":name[:100],"city":"Türkiye","country":"Türkiye"}}}
    return iyzico_request("POST","/v2/subscription/checkoutform/initialize",payload)

def verify_iyzico_subscription_webhook(data, signature):
    secret=os.environ.get("IYZICO_SECRET_KEY","").strip()
    merchant=str(data.get("merchantId","")).strip()
    event=str(data.get("iyziEventType","")).strip()
    sub=str(data.get("subscriptionReferenceCode","")).strip()
    order=str(data.get("orderReferenceCode","")).strip()
    customer=str(data.get("customerReferenceCode","")).strip()
    if not secret or not merchant or not event or not sub or not order or not customer or not signature:
        return False
    key=merchant+secret+event+sub+order+customer
    calculated=hmac.new(secret.encode("utf-8"),key.encode("utf-8"),hashlib.sha256).hexdigest()
    return hmac.compare_digest(calculated,signature.strip())

def iyzico_cancel_subscription(reference_code):
    return iyzico_request("POST",f"/v2/subscription/subscriptions/{reference_code}/cancel",{"subscriptionReferenceCode":reference_code})

def user_can(user, module):
    if not user or not database_enabled(): return False
    if user[3] == 'owner': return True
    permissions = database.get_user_permissions(user[0]) or {}
    return bool(permissions.get(module, False))


def business_config_for_user(user):
    value = user[5]
    return json.loads(value) if isinstance(value, str) else value


def initialize_database():
    if database_enabled():
        database.ensure_schema()
        cleanup_prefix = os.environ.get("NEXORA_DELETE_EMAIL_PREFIX", "").strip()
        if cleanup_prefix:
            deleted = database.delete_users_by_email_prefix(cleanup_prefix)
            print(f"NEXORA one-time account cleanup: {deleted} account(s) removed for prefix {cleanup_prefix}")
        config = load_config()
        business_id = database.get_business_id(config)
        legacy_appointments = load_json(APPOINTMENTS_PATH, [])
        legacy_messages = load_json(MESSAGES_PATH, [])
        current_appointments = database.load_appointments(config)
        current_messages = database.load_messages(config)
        if not current_appointments and legacy_appointments:
            database.save_appointments(config, legacy_appointments)
        if not current_messages and legacy_messages:
            database.save_messages(config, legacy_messages)

def require_permission(handler, module):
    user = auth_required(handler)
    if not user:
        return None
    if not user_can(user, module):
        handler.send_json(403, {"error": "Bu işlem için yetkiniz yok."})
        return None
    return user

def load_appointments(user=None):
    if database_enabled():
        return database.load_appointments_by_business(user[1]) if user else database.load_appointments(load_config())
    return load_json(APPOINTMENTS_PATH, [])

def save_appointments(items, user=None):
    if database_enabled():
        database.save_appointments_by_business(user[1], items) if user else database.save_appointments(load_config(), items)
    else:
        save_json(APPOINTMENTS_PATH, items)

def load_messages(user=None):
    if database_enabled():
        return database.load_messages_by_business(user[1]) if user else database.load_messages(load_config())
    return load_json(MESSAGES_PATH, [])

def save_messages(items, user=None):
    if database_enabled():
        database.save_messages_by_business(user[1], items) if user else database.save_messages(load_config(), items)
    else:
        save_json(MESSAGES_PATH, items)

def gemini_request(prompt, max_tokens=1000):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY tanımlı değil.")
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": max_tokens}}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        data = json.load(response)
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

def ask_gemini(message, config):
    prompt = f"""{SYSTEM_PROMPT}

İŞLETME BİLGİLERİ:
{json.dumps(config, ensure_ascii=False, indent=2)}

MÜŞTERİ MESAJI:
{message}

Sadece müşteriye gönderilebilecek cevabı üret."""
    return gemini_request(prompt, 1200)

def detect_appointment_intent(message, config):
    prompt = f"""Bir işletme mesajını randevu açısından sınıflandır.
Sadece geçerli JSON döndür, Markdown kullanma.
Alanlar:
intent: "appointment" veya "other"
customer_name: mesajda varsa isim, yoksa ""
phone: mesajda varsa telefon, yoksa ""
date: açıkça belirtilen tarih varsa YYYY-MM-DD, yoksa ""
time: açıkça belirtilen saat varsa HH:MM, yoksa ""
service: işletmenin hizmetlerinden biri veya mesajdaki hizmet, yoksa ""
note: kısa not

İŞLETME:
{json.dumps(config, ensure_ascii=False)}

MESAJ:
{message}
"""
    raw = gemini_request(prompt, 600)
    try:
        start, end = raw.find("{"), raw.rfind("}")
        return json.loads(raw[start:end + 1])
    except Exception:
        return {"intent": "other", "customer_name": "", "phone": "", "date": "", "time": "", "service": "", "note": ""}

def create_appointment(data, user=None):
    appointment = {
        "id": datetime.utcnow().strftime("%Y%m%d%H%M%S%f"),
        "customer_name": str(data.get("customer_name", "")).strip(),
        "phone": str(data.get("phone", "")).strip(),
        "date": str(data.get("date", "")).strip(),
        "time": str(data.get("time", "")).strip(),
        "service": str(data.get("service", "")).strip(),
        "note": str(data.get("note", "")).strip(),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    required = ["customer_name", "phone", "date", "time", "service"]
    if any(not appointment[x] for x in required):
        return None, "customer_name, phone, date, time ve service zorunlu"

    config = business_config_for_user(user) if user else load_config()
    try:
        requested_date = datetime.strptime(appointment["date"], "%Y-%m-%d").date()
    except ValueError:
        return None, "Geçerli bir tarih seçin."
    if requested_date < datetime.now().date():
        return None, "Geçmiş bir tarihe randevu oluşturulamaz."
    if is_closed_day(appointment["date"], config):
        return None, "Seçilen gün işletme kapalı."
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", appointment["time"]):
        return None, "Geçerli bir saat seçin."
    if not is_time_available(appointment["date"], appointment["time"], config, user):
        return None, "Seçilen tarih ve saat uygun değil."

    items = load_appointments(user)
    active = {"pending", "confirmed"}
    conflict = next((item for item in items if item.get("date") == appointment["date"] and item.get("time") == appointment["time"] and item.get("status") in active), None)
    if conflict:
        return None, "Bu tarih ve saatte başka bir randevu bulunuyor."

    items.append(appointment)
    save_appointments(items, user)
    return appointment, None

def appointment_time_from_text(text):
    match = re.search(r"\b([01]?\d|2[0-3])(?:[:.]([0-5]\d))?\b", text)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or "00")
    return f"{hour:02d}:{minute:02d}"

def resolve_relative_date(text):
    today = datetime.now().date()
    if "bugün" in text:
        return today.strftime("%Y-%m-%d")
    if "yarın" in text:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if "öbür gün" in text:
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")
    return ""

def local_intent_hint(message, config):
    text = message.lower()
    time_text = appointment_time_from_text(text)
    date_text = resolve_relative_date(text)
    appointment_words = ("randevu", "rezervasyon", "uygun saat", "saat", "boş mu", "boş", "müsait", "uygun")
    if any(word in text for word in appointment_words):
        service = ""
        for item in config.get("services", []):
            name = str(item.get("name", "")).strip()
            if name and name.lower() in text:
                service = name
                break

        date_value = date_text or (datetime.now().strftime("%Y-%m-%d") if time_text and any(word in text for word in ("boş", "müsait", "uygun")) else "")

        return {
            "intent": "availability" if any(word in text for word in ("boş", "müsait", "uygun")) else "appointment",
            "customer_name": "",
            "phone": "",
            "date": date_value,
            "time": time_text,
            "service": service,
            "note": ""
        }

    for item in config.get("services", []):
        name = str(item.get("name", "")).strip()
        price = str(item.get("price", "")).strip()
        normalized = name.lower().replace("+", " ")
        if name and price and (
            name.lower() in text or (normalized.replace(" ", "") == "saçsakal" and "saç sakal" in text)
        ) and any(word in text for word in ("fiyat", "ne kadar", "ücret", "kaç tl")):
            return {"intent": "price", "service": name, "price": price}
    return None

def is_closed_day(date, config):
    try:
        day_name = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"][datetime.strptime(date, "%Y-%m-%d").weekday()]
    except ValueError:
        return False
    return day_name in {str(day).strip() for day in config.get("closed_days", [])}

def is_time_available(date, time, config=None, user=None):
    if not date or not time:
        return None
    config = config or load_config()
    if is_closed_day(date, config):
        return False
    hours = str(config.get("working_hours", "09:00-18:00"))
    match = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", hours)
    if not match:
        return False
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    try:
        requested = int(time[:2]) * 60 + int(time[3:5])
    except (ValueError, TypeError):
        return False
    if requested < start or requested >= end:
        return False
    items = load_appointments(user)
    active = {"pending", "confirmed"}
    return not any(item.get("date") == date and item.get("time") == time and item.get("status") in active for item in items)

def get_free_slots(date, config, user=None):
    if is_closed_day(date, config):
        return []
    hours = str(config.get("working_hours", "09:00-18:00"))
    match = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", hours)
    if not match:
        return []
    start = int(match.group(1))
    end = int(match.group(3))
    first_hour = start
    if date == datetime.now().strftime("%Y-%m-%d"):
        first_hour = max(start, datetime.now().hour + (1 if datetime.now().minute else 0))
    return [f"{hour:02d}:00" for hour in range(first_hour, end) if is_time_available(date, f"{hour:02d}:00", config, user)]

def handle_customer_message(message, channel="web", customer_name="", phone="", user=None):
    config = business_config_for_user(user) if user else load_config()
    hint = local_intent_hint(message, config)
    intent = hint if hint else detect_appointment_intent(message, config)
    record = {
        "id": datetime.utcnow().strftime("%Y%m%d%H%M%S%f"),
        "channel": channel,
        "direction": "inbound",
        "customer_name": customer_name or intent.get("customer_name", ""),
        "phone": phone or intent.get("phone", ""),
        "message": message,
        "intent": intent.get("intent", "other"),
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    messages = load_messages(user)
    messages.append(record)
    save_messages(messages, user)

    if intent.get("intent") == "price":
        return {"reply": f"{intent.get('service')} fiyatı {intent.get('price')}."}

    if intent.get("intent") == "availability":
        date = intent.get("date", "")
        time = intent.get("time", "")
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        if is_closed_day(date, config):
            return {"reply": f"{date} günü işletme kapalıdır. Pazar günleri tatildir."}
        if not time:
            slots = get_free_slots(date, config, user)
            if slots:
                return {"reply": f"Bugün ({date}) boş saatler: " + ", ".join(slots) + "."}
            return {"reply": f"Bugün ({date}) için uygun boş saat görünmüyor."}
        available = is_time_available(date, time, config, user)
        if available:
            return {"reply": f"Evet, {date} günü saat {time} şu an boş görünüyor."}
        return {"reply": f"Maalesef {date} günü saat {time} uygun değil. Çalışma saatleri 09:00-18:00, Pazar günleri tatil."}

    if intent.get("intent") == "appointment":
        intent["customer_name"] = record["customer_name"]
        intent["phone"] = record["phone"]
        required = ["customer_name", "phone", "date", "time", "service"]
        missing = [x for x in required if not str(intent.get(x, "")).strip()]
        if not missing:
            if is_closed_day(intent["date"], config):
                return {"reply": "Pazar günü işletme kapalı. Lütfen başka bir gün seçer misiniz?"}
            if not is_time_available(intent["date"], intent["time"], config, user):
                return {"reply": "Seçtiğiniz tarih veya saat uygun değil. Çalışma saatleri 09:00-18:00, Pazar günleri tatil."}
            appointment, error = create_appointment(intent, user)
            if appointment:
                return {"reply": f"Randevunuzu aldım. {appointment['date']} {appointment['time']} için {appointment['service']} kaydınız oluşturuldu.", "appointment": appointment}
            if error == "Bu tarih ve saatte başka bir randevu bulunuyor.":
                return {"reply": "Bu tarih ve saatte başka bir randevu bulunuyor. Lütfen farklı bir saat seçer misiniz?"}
        labels = {"customer_name":"adınızı","phone":"telefon numaranızı","date":"tarihi","time":"saati","service":"hizmeti"}
        return {"reply": "Randevu oluşturabilmem için lütfen " + ", ".join(labels[x] for x in missing) + " bilgisini de paylaşır mısınız?"}

    return {"reply": ask_gemini(message, config)}

def send_whatsapp_text(to, message, business_id=None):
    token = ""
    phone_number_id = ""
    if business_id is not None and database_enabled():
        credentials = database.get_business_whatsapp(business_id)
        token = str(credentials.get("access_token", "")).strip()
        phone_number_id = str(credentials.get("phone_number_id", "")).strip()
    # Backward-compatible fallback for the original single-business deployment.
    if not token:
        token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    if not phone_number_id:
        phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    if not token or not phone_number_id:
        return False, "Bu işletme için WhatsApp bağlantısı yapılandırılmamış."

    url = f"https://graph.facebook.com/v23.0/{phone_number_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": False, "body": message}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return True, json.load(response)
    except Exception as exc:
        return False, str(exc)

def whatsapp_verify(query):
    params = parse_qs(query)
    mode = params.get("hub.mode", [""])[0]
    token = params.get("hub.verify_token", [""])[0]
    challenge = params.get("hub.challenge", [""])[0]
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if mode == "subscribe" and expected and token == expected:
        return 200, challenge
    return 403, "Webhook doğrulaması başarısız"

def whatsapp_business_id(data):
    try:
        phone_number_id = str(data["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]).strip()
    except (KeyError, IndexError, TypeError):
        return None
    if database_enabled() and phone_number_id:
        business_id = database.find_business_by_whatsapp_phone_number_id(phone_number_id)
        if business_id:
            return business_id
    # Backward-compatible fallback for legacy single/multi-business env mapping.
    mapping = os.environ.get("WHATSAPP_BUSINESS_MAP", "").strip()
    if not mapping:
        return None
    try:
        parsed = json.loads(mapping)
        value = parsed.get(phone_number_id)
        return int(value) if value is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

def verify_whatsapp_signature(raw_body, signature):
    secret = os.environ.get("META_APP_SECRET", "").strip()
    if not secret:
        return False
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

def whatsapp_message_already_processed(message_id, business_id=None):
    if not message_id or business_id is None:
        return False
    return any(item.get("message_id") == message_id for item in database.load_messages_by_business(business_id))

def parse_whatsapp_message(data):
    try:
        value = data["entry"][0]["changes"][0]["value"]
        messages = value.get("messages", [])
        if not messages:
            return None
        msg = messages[0]
        if msg.get("type") != "text":
            return None
        return {"message": msg["text"]["body"], "phone": msg.get("from", ""), "customer_name": value.get("contacts", [{}])[0].get("profile", {}).get("name", ""), "message_id": msg.get("id", "")}
    except (KeyError, IndexError, TypeError):
        return None

class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        # Baseline browser security headers for the SaaS surface.
        # CSP is intentionally omitted here because the current frontend uses
        # inline styles/scripts; it can be tightened after a nonce-based migration.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        static_types = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/service-worker.js": ("service-worker.js", "application/javascript; charset=utf-8"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
            "/app-icon.svg": ("app-icon.svg", "image/svg+xml"),
        }
        if parsed.path in static_types:
            filename, content_type = static_types[parsed.path]
            try:
                size = os.path.getsize(filename)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache" if filename in {"index.html", "service-worker.js", "manifest.webmanifest"} else "public, max-age=86400")
                self.send_header("Content-Length", str(size))
                self.end_headers()
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
            return
        if parsed.path == "/health":
            body = b'{"status":"ok","service":"AI \\u0130\\u015fletme Asistan\\u0131"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        origin = self.headers.get("Origin", "")
        allowed = os.environ.get("ALLOWED_ORIGIN", "").strip()
        if allowed and origin == allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()


    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/webhook/whatsapp":
            status, body = whatsapp_verify(parsed.query)
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/service-worker.js": ("service-worker.js", "application/javascript; charset=utf-8"),
            "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
            "/app-icon.svg": ("app-icon.svg", "image/svg+xml"),
        }
        if parsed.path in static_files:
            filename, content_type = static_files[parsed.path]
            try:
                with open(filename, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-cache" if filename in {"index.html", "service-worker.js", "manifest.webmanifest"} else "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_json(404, {"error": filename + " bulunamadı"})
            return
        if parsed.path == "/billing/iyzico/callback":
            token=(parse_qs(parsed.query).get("token") or parse_qs(parsed.query).get("checkoutFormToken") or [""])[0]
            safe=json.dumps(token)
            body='<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>NEXORA Ödeme</title></head><body style="font-family:Arial;padding:40px;text-align:center"><h2>NEXORA</h2><p>Ödeme sonucu kontrol ediliyor...</p><script>fetch("/api/billing/complete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:'+safe+'})}).then(()=>location.href="/").catch(()=>location.href="/");</script></body></html>'
            self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body.encode("utf-8")))); self.end_headers(); self.wfile.write(body.encode("utf-8")); return

        if parsed.path == "/api/master/status":
            user=master_admin_required(self)
            if not user: return
            self.send_json(200,{"enabled":True,"email":user[2]}); return
        if parsed.path == "/api/master/stats":
            user=master_admin_required(self)
            if not user: return
            self.send_json(200,master_stats()); return
        if parsed.path == "/api/master/businesses":
            user=master_admin_required(self)
            if not user: return
            self.send_json(200,master_businesses()); return
        if parsed.path == "/api/health/db":
            configured=database_enabled()
            reachable=False
            error_type=""
            if configured:
                try:
                    with database.connection() as conn:
                        conn.execute("SELECT 1").fetchone()
                    reachable=True
                except Exception as exc:
                    error_type=type(exc).__name__
            self.send_json(200,{"database_configured":configured,"database_reachable":reachable,"error_type":error_type})
            return
        if parsed.path == "/api/auth/status":
            user=current_user(self)
            payload={"authenticated":bool(user),"email":user[2] if user else ""}
            if user and database_enabled():
                payload["subscription"]=database.get_business_subscription(user[1])
            self.send_json(200,payload); return
        if parsed.path == "/api/auth/setup-available":
            self.send_json(200,{"available":database_enabled() and database.count_users()==0}); return
        if parsed.path == "/api/plans":
            price=os.environ.get("NEXORA_PRO_PRICE","").strip()
            self.send_json(200,{"plans":[{"id":"trial","name":"14 Gün Deneme","price":0,"features":["Randevu yönetimi","Müşteri yönetimi","AI müşteri asistanı","Ekip ve yetkiler"]},{"id":"pro","name":"NEXORA Pro","price":price or None,"price_note":"" if price else "Fiyatlandırma yapılandırılıyor","features":["Tüm deneme özellikleri","WhatsApp entegrasyonu","Gelişmiş raporlar","Öncelikli destek"]}]}); return
        if parsed.path == "/oauth/google/callback":
            q=parse_qs(parsed.query); state=(q.get("state") or [""])[0]; code=(q.get("code") or [""])[0]
            if not state or not code or not google_calendar:
                self.send_response(302); self.send_header("Location","/?calendar_error=1"); self.end_headers(); return
            try:
                google_calendar.connect_from_callback(state,code)
                self.send_response(302); self.send_header("Location","/?calendar_connected=1"); self.end_headers(); return
            except Exception:
                self.send_response(302); self.send_header("Location","/?calendar_error=1"); self.end_headers(); return

        if parsed.path == "/api/calendar/status":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"business_settings"):
                self.send_json(403,{"error":"İşletme ayarlarına erişim yetkiniz yok."}); return
            self.send_json(200,google_calendar.status(user[1]) if google_calendar else {"connected":False}); return

        if parsed.path == "/api/calendar/connect":
            user=auth_required(self)
            if not user: return
            if user[3] != "owner":
                self.send_json(403,{"error":"Sadece işletme sahibi Google Calendar bağlayabilir."}); return
            if not google_calendar or not google_calendar.configured():
                self.send_json(503,{"error":"Google Calendar OAuth ayarları henüz yapılandırılmadı.","setup_required":True}); return
            self.send_json(200,{"url":google_calendar.authorization_url(user[1],user[0])}); return

        if parsed.path == "/api/subscription":
            user=session_required(self)
            if not user: return
            self.send_json(200,subscription_payload(user[1])); return
        if parsed.path == "/api/usage":
            user=session_required(self)
            if not user: return
            self.send_json(200,usage_payload(user[1])); return
        if parsed.path == "/api/business":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"business_settings"):
                self.send_json(403,{"error":"İşletme ayarlarına erişim yetkiniz yok."}); return
            config=dict(business_config_for_user(user))
            whatsapp=database.get_business_whatsapp(user[1]) if database_enabled() else {"phone_number_id":"","access_token":""}
            config["whatsapp_phone_number_id"]=whatsapp.get("phone_number_id","")
            config["whatsapp_connected"]=bool(whatsapp.get("phone_number_id") and whatsapp.get("access_token"))
            config.pop("whatsapp_access_token", None)
            self.send_json(200, config); return
        if parsed.path == "/api/appointments":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"appointments"):
                self.send_json(403,{"error":"Randevulara erişim yetkiniz yok."}); return
            self.send_json(200, load_appointments(user)); return
        if parsed.path == "/api/messages":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"messages"):
                self.send_json(403,{"error":"Mesajlara erişim yetkiniz yok."}); return
            self.send_json(200, load_messages(user)); return
        if parsed.path == "/api/customers":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"customers"):
                self.send_json(403,{"error":"Müşterilere erişim yetkiniz yok."}); return
            rows=database.list_customers_by_business(user[1])
            self.send_json(200,[{"id":r[0],"name":r[1],"phone":r[2],"created_at":r[3].isoformat() if hasattr(r[3],"isoformat") else str(r[3]),"updated_at":r[4].isoformat() if hasattr(r[4],"isoformat") else str(r[4])} for r in rows]); return
        if parsed.path == "/api/reports":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"reports"):
                self.send_json(403,{"error":"Raporlara erişim yetkiniz yok."}); return
            self.send_json(200,database.get_business_report(user[1])); return
        if parsed.path == "/api/users":
            user=auth_required(self)
            if not user: return
            if not user_can(user,"team"):
                self.send_json(403,{"error":"Ekip bilgilerine erişim yetkiniz yok."}); return
            rows=database.list_users(user[1])
            self.send_json(200,[{"id":r[0],"email":r[1],"role":r[2],"created_at":r[3].isoformat() if hasattr(r[3],"isoformat") else str(r[3])} for r in rows]); return
        if parsed.path == "/api/users/permissions" and self.command == "GET":
            user=auth_required(self)
            if not user: return
            if user[3] != "owner":
                self.send_json(403,{"error":"Sadece işletme sahibi yetkileri görüntüleyebilir."}); return
            try: target=int(parse_qs(parsed.query).get("user_id",["0"])[0])
            except Exception: target=0
            target_user=database.get_user_in_business(user[1],target)
            if not target_user:
                self.send_json(404,{"error":"Çalışan bulunamadı"}); return
            self.send_json(200,database.get_user_permissions(user[1],target) or {}); return
        if parsed.path == "/api/audit":
            user=auth_required(self)
            if not user:
                return
            if user[3] != "owner":
                self.send_json(403,{"error":"Sadece owner denetim kayıtlarını görebilir."})
                return
            rows=database.list_audit_logs(user[1])
            self.send_json(200,[{"id":x[0],"user_id":x[1],"action":x[2],"target_type":x[3],"target_id":x[4],"details":x[5],"created_at":x[6].isoformat()} for x in rows])
            return
        if parsed.path == "/health":
            db_ok = False
            if database_enabled():
                try:
                    database.ensure_schema()
                    db_ok = True
                except Exception:
                    db_ok = False
            self.send_json(200, {
                "status": "ok" if db_ok else "degraded",
                "service": "NEXORA",
                "database": "ok" if db_ok else "unavailable"
            }); return
        self.send_json(404, {"error": "Not found"})

    def csrf_request_allowed(self):
        """Allow cookie-authenticated state changes only from our own origin."""
        origin = self.headers.get("Origin", "").strip().rstrip("/")
        referer = self.headers.get("Referer", "").strip()
        allowed = os.environ.get("ALLOWED_ORIGIN", "").strip().rstrip("/")
        if not allowed:
            host = self.headers.get("Host", "").strip()
            if not host:
                return False
            allowed = "https://" + host
        if origin:
            return hmac.compare_digest(origin, allowed)
        if referer:
            return referer.startswith(allowed + "/") or referer == allowed
        return False

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY_BYTES:
                self.send_json(413, {"error":"İstek gövdesi çok büyük."})
                return
            raw_body = self.rfile.read(length) or b"{}"

            if self.path == "/webhook/whatsapp" and not verify_whatsapp_signature(raw_body, self.headers.get("X-Hub-Signature-256", "")):
                self.send_json(403, {"error": "WhatsApp webhook imza doğrulaması başarısız"})
                return

            # Browser session endpoints must be same-origin. Login/register are
            # intentionally excluded because they do not yet have an authenticated session.
            csrf_exempt = {
                "/api/auth/register",
                "/api/auth/login",
                "/webhook/whatsapp",
                "/webhook/iyzico/subscription",
                "/webhook/billing",
            }
            if self.path not in csrf_exempt and cookie_token(self) and not self.csrf_request_allowed():
                self.send_json(403, {"error": "İstek kaynağı doğrulanamadı."})
                return

            data = json.loads(raw_body)

            if self.path == "/internal/reminders":
                expected = os.environ.get("NEXORA_REMINDER_CRON_SECRET", "").strip()
                provided = self.headers.get("X-Nexora-Cron-Secret", "").strip()
                if not expected or not provided or not hmac.compare_digest(expected, provided):
                    self.send_json(403, {"error": "Hatırlatma worker doğrulaması başarısız."})
                    return
                try:
                    from reminder_worker import main as run_reminders
                    run_reminders()
                    self.send_json(200, {"success": True})
                except Exception as exc:
                    self.send_json(500, {"error": "Hatırlatma worker çalıştırılamadı.", "detail": str(exc)})
                return

            if self.path == "/api/calendar/disconnect":
                user=auth_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi bağlantıyı kaldırabilir."}); return
                google_calendar.disconnect(user[1])
                database.write_audit_log(user[1],user[0],"google_calendar_disconnected","calendar","",{})
                self.send_json(200,{"success":True}); return

            if self.path == "/api/calendar/sync":
                user=auth_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi senkronizasyon başlatabilir."}); return
                if not google_calendar or not google_calendar.status(user[1]).get("connected"):
                    self.send_json(400,{"error":"Google Calendar bağlı değil."}); return
                results=google_calendar.sync_all(user[1],load_appointments(user))
                database.write_audit_log(user[1],user[0],"google_calendar_synced","calendar","",{"count":len(results)})
                self.send_json(200,{"success":True,"results":results}); return

            if self.path == "/api/billing/complete":
                user=session_required(self)
                if not user: return
                token=str(data.get("token","")).strip()
                session=database.get_billing_checkout_session(token)
                if not token or not session or session[1] != user[1]:
                    self.send_json(404,{"error":"Ödeme oturumu bulunamadı."}); return
                result=iyzico_request("GET","/v2/subscription/checkoutform/"+token)
                if result.get("status") != "success":
                    self.send_json(502,{"error":result.get("errorMessage","Ödeme sonucu alınamadı."),"provider_response":result}); return
                info=result.get("data") or {}
                sub_ref=str(info.get("referenceCode","")).strip()
                customer_ref=str(info.get("customerReferenceCode","")).strip()
                sub_status=str(info.get("subscriptionStatus","")).lower()
                if not sub_ref:
                    self.send_json(502,{"error":"iyzico abonelik referansı alınamadı."}); return
                database.create_or_update_subscription(user[1],"iyzico",customer_ref,sub_ref,"active" if sub_status=="active" else "pending")
                database.complete_billing_checkout(token)
                database.write_audit_log(user[1],user[0],"subscription_started","subscription",sub_ref,{"provider":"iyzico"})
                self.send_json(200,{"success":True,"subscription":database.get_subscription_record(user[1])}); return

            if self.path == "/webhook/iyzico/subscription":
                signature=self.headers.get("X-IYZ-SIGNATURE-V3","")
                if not verify_iyzico_subscription_webhook(data,signature):
                    self.send_json(403,{"error":"iyzico webhook imzası doğrulanamadı."}); return
                business_id=database.get_business_by_provider_subscription(str(data.get("subscriptionReferenceCode","")).strip())
                if not business_id:
                    self.send_json(404,{"error":"Abonelik eşleşmesi bulunamadı."}); return
                event=str(data.get("iyziEventType","")).strip()
                status="active" if event=="subscription.order.success" else "past_due"
                database.update_subscription_status(business_id,status)
                self.send_json(200,{"success":True}); return

            if self.path == "/webhook/billing":
                signature=self.headers.get("X-Nexora-Signature","")
                if not verify_billing_signature(raw_body,signature):
                    self.send_json(403,{"error":"Ödeme webhook imzası doğrulanamadı."}); return
                business_id=int(data.get("business_id",0))
                status=str(data.get("status","")).strip().lower()
                if business_id <= 0 or status not in {"active","trialing","cancelled","canceled","past_due","unpaid","inactive"}:
                    self.send_json(400,{"error":"Geçersiz ödeme webhook verisi."}); return
                database.create_or_update_subscription(business_id,str(data.get("provider","")).strip(),str(data.get("provider_customer_id","")).strip(),str(data.get("provider_subscription_id","")).strip(),status=status,period_start=data.get("current_period_start"),period_end=data.get("current_period_end"),cancel_at_period_end=bool(data.get("cancel_at_period_end",False)))
                self.send_json(200,{"success":True}); return

            if self.path == "/api/billing/start":
                user=session_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi abonelik başlatabilir."}); return
                try:
                    provider=os.environ.get("NEXORA_PAYMENT_PROVIDER","iyzico").strip().lower()
                    if provider != "iyzico":
                        raise RuntimeError("Desteklenen ödeme sağlayıcısı: iyzico.")
                    result=iyzico_start_checkout(user)
                    if result.get("status") != "success":
                        self.send_json(502,{"error":result.get("errorMessage","iyzico ödeme formu oluşturulamadı."),"provider_response":result}); return
                    token=result.get("token","")
                    conversation=result.get("conversationId","")
                    if not token:
                        self.send_json(502,{"error":"iyzico ödeme tokenı alınamadı."}); return
                    database.create_billing_checkout_session(user[1],"iyzico",token,conversation)
                    self.send_json(200,{"provider":"iyzico","checkout_form_content":result.get("checkoutFormContent",""),"token":token,"conversation_id":conversation})
                except Exception as exc:
                    self.send_json(503,{"error":str(exc),"setup_required":True})
                return

            if self.path == "/api/billing/cancel":
                user=session_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi aboneliği yönetebilir."}); return
                record=database.get_subscription_record(user[1])
                if not record:
                    self.send_json(404,{"error":"Aktif abonelik bulunamadı."}); return
                if record.get("provider") == "iyzico" and record.get("provider_subscription_id"):
                    try:
                        result=iyzico_cancel_subscription(record["provider_subscription_id"])
                        if result.get("status") != "success":
                            self.send_json(502,{"error":result.get("errorMessage","iyzico abonelik iptali başarısız."),"provider_response":result}); return
                    except Exception as exc:
                        self.send_json(502,{"error":str(exc)}); return
                database.update_subscription_status(user[1],"cancelled",cancel_at_period_end=True,period_end=record.get("current_period_end"))
                database.write_audit_log(user[1],user[0],"subscription_cancelled","subscription",record.get("id"),{"provider":record.get("provider","")})
                self.send_json(200,{"success":True}); return

            if self.path == "/api/auth/forgot-password":
                email=str(data.get("email","")).strip().lower()
                if len(email)>254 or not email or "@" not in email:
                    self.send_json(200,{"success":True,"message":"Eğer hesap varsa sıfırlama bağlantısı e-posta adresinize gönderildi."}); return
                user=database.get_user_by_email(email) if database_enabled() else None
                if user:
                    token,token_hash=make_one_time_token()
                    database.create_password_reset_token(token_hash,user[0],datetime.now(timezone.utc)+timedelta(minutes=30))
                    base=frontend_base_url()
                    if base:
                        if not send_email(user[2],"NEXORA şifre sıfırlama",f"NEXORA şifrenizi yenilemek için bağlantı:\n{base}/?reset_token={token}\n\nBağlantı 30 dakika geçerlidir."):
                            print("PASSWORD_RESET_EMAIL_FAILED: SMTP/Resend yapılandırması veya gönderim başarısız.", flush=True)
                self.send_json(200,{"success":True,"message":"Eğer hesap varsa sıfırlama bağlantısı e-posta adresinize gönderildi."}); return

            if self.path == "/api/auth/reset-password":
                token=str(data.get("token","")).strip()
                password=str(data.get("password",""))
                if len(token)<40 or len(password)<8 or len(password)>128:
                    self.send_json(400,{"error":"Geçersiz sıfırlama bilgisi."}); return
                user_id=database.consume_password_reset_token(hashlib.sha256(token.encode("utf-8")).hexdigest()) if database_enabled() else None
                if not user_id:
                    self.send_json(400,{"error":"Sıfırlama bağlantısı geçersiz veya süresi dolmuş."}); return
                database.set_user_password(user_id,hash_password(password))
                self.send_json(200,{"success":True,"message":"Şifreniz güncellendi. Yeni şifrenizle giriş yapabilirsiniz."}); return

            if self.path == "/api/auth/verify-email":
                token=str(data.get("token","")).strip()
                user_id=database.consume_email_verification_token(hashlib.sha256(token.encode("utf-8")).hexdigest()) if database_enabled() and len(token)>=40 else None
                if not user_id:
                    self.send_json(400,{"error":"Doğrulama bağlantısı geçersiz veya süresi dolmuş."}); return
                database.mark_email_verified(user_id)
                self.send_json(200,{"success":True,"message":"E-posta adresiniz doğrulandı."}); return

            if self.path == "/api/auth/register":
                client_key=self.client_address[0] if self.client_address else "unknown"
                if rate_limited("register:"+client_key, REGISTER_RATE_LIMIT, REGISTER_RATE_WINDOW):
                    self.send_json(429,{"error":"Çok fazla kayıt denemesi. Lütfen daha sonra tekrar deneyin."}); return
                email=str(data.get("email","")).strip().lower()
                password=str(data.get("password",""))
                business_name=str(data.get("business_name","")).strip()
                if len(email) > 254 or len(password) > 128 or len(business_name) > 120:
                    self.send_json(400,{"error":"Girilen bilgiler izin verilen uzunluğu aşıyor."}); return
                if not email or "@" not in email or len(password) < 8 or not business_name:
                    self.send_json(400,{"error":"İşletme adı, geçerli e-posta ve en az 8 karakterli şifre gerekli."}); return
                if not database_enabled():
                    self.send_json(503,{"error":"Veritabanı hazır değil."}); return
                if database.get_user_by_email(email):
                    self.send_json(409,{"error":"Bu e-posta zaten kayıtlı. Giriş yapmayı deneyin."}); return
                config=dict(DEFAULT_CONFIG)
                config["business_name"]=business_name
                business_id=database.create_business(business_name,config)
                user_id=database.create_user(email,hash_password(password),business_id,"owner")
                verification_token,verification_hash=make_one_time_token()
                database.create_email_verification_token(verification_hash,user_id,datetime.now(timezone.utc)+timedelta(hours=24))
                base=frontend_base_url()
                if base:
                    try:
                        send_email(email,"NEXORA e-posta doğrulama",f"NEXORA hesabınızı doğrulamak için bağlantı:\n{base}/?verify_token={verification_token}\n\nBağlantı 24 saat geçerlidir.")
                    except Exception:
                        pass
                token=os.urandom(32).hex()
                expires=None
                database.create_session(hashlib.sha256(token.encode("utf-8")).hexdigest(),user_id,expires)
                self.send_response(201)
                self.send_header("Set-Cookie","session="+token+"; HttpOnly; SameSite=Lax; Path=/; Secure; Max-Age=315360000")
                self.send_header("Content-Type","application/json; charset=utf-8")
                body=json.dumps({"success":True,"email":email},ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return

            if self.path == "/api/auth/login":
                client_key=self.client_address[0] if self.client_address else "unknown"
                if rate_limited("login:"+client_key,8,60):
                    self.send_json(429,{"error":"Çok fazla giriş denemesi. Lütfen kısa süre sonra tekrar deneyin."}); return
                email=str(data.get("email","")).strip().lower()
                password=str(data.get("password",""))
                if not email or not password:
                    self.send_json(400,{"error":"E-posta ve şifre gerekli."}); return
                if not database_enabled():
                    self.send_json(503,{"error":"Veritabanı hazır değil."}); return
                user=database.get_user_by_email(email)
                if not user or not verify_password(password,user[3]):
                    self.send_json(401,{"error":"E-posta veya şifre hatalı."}); return
                token=os.urandom(32).hex()
                expires=None
                database.create_session(hashlib.sha256(token.encode("utf-8")).hexdigest(),user[0],expires)
                self.send_response(200)
                self.send_header("Set-Cookie","session="+token+"; HttpOnly; SameSite=Lax; Path=/; Secure; Max-Age=315360000")
                self.send_header("Content-Type","application/json; charset=utf-8")
                body=json.dumps({"success":True,"email":user[2]},ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return

            if self.path == "/api/auth/delete-account":
                user=session_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi hesabını silebilir."}); return
                password=str(data.get("password",""))
                confirm=str(data.get("confirmation","")).strip().upper()
                if len(password) < 8 or len(password) > 128:
                    self.send_json(400,{"error":"Mevcut şifrenizi girin."}); return
                if confirm != "SİL":
                    self.send_json(400,{"error":"Silme işlemini onaylamak için SİL yazmalısınız."}); return
                account=database.get_user_by_email(user[2])
                if not account or not verify_password(password,account[3]):
                    self.send_json(401,{"error":"Mevcut şifre hatalı."}); return
                if database_enabled():
                    sub=database.get_business_subscription(user[1])
                    if str(sub.get("plan","trial")).lower()=="pro" and str(sub.get("status","")).lower()=="active":
                        self.send_json(409,{"error":"Aktif Pro aboneliğiniz var. Hesabı silmeden önce aboneliği iptal edin."}); return
                    deleted=database.delete_account(user[0],user[1])
                    if not deleted:
                        self.send_json(404,{"error":"Hesap bulunamadı."}); return
                self.send_response(200)
                self.send_header("Set-Cookie","session=; HttpOnly; SameSite=Lax; Path=/; Secure; Max-Age=0")
                self.send_header("Content-Type","application/json; charset=utf-8")
                body=json.dumps({"success":True,"message":"NEXORA hesabınız ve işletme verileriniz kalıcı olarak silindi."},ensure_ascii=False).encode("utf-8")
                self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return

            if self.path == "/api/auth/logout":
                token=cookie_token(self)
                if token and database_enabled():
                    database.revoke_session(hashlib.sha256(token.encode("utf-8")).hexdigest())
                self.send_response(200)
                self.send_header("Set-Cookie","session=; HttpOnly; SameSite=Lax; Path=/; Secure; Max-Age=0")
                self.send_header("Content-Type","application/json; charset=utf-8")
                body=b'{"success":true}'
                self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return

            if self.path == "/webhook/whatsapp":
                business_id = whatsapp_business_id(data)
                if business_id is None:
                    self.send_json(403,{"error":"WhatsApp işletme eşlemesi bulunamadı."}); return
                incoming = parse_whatsapp_message(data)
                if not incoming:
                    self.send_json(200, {"received": True, "processed": False}); return
                if whatsapp_message_already_processed(incoming.get("message_id", ""), business_id):
                    self.send_json(200, {"received": True, "processed": False, "duplicate": True}); return
                whatsapp_user = (0, business_id, "", "owner", "", database.get_business_config(business_id))
                result = handle_customer_message(incoming["message"], "whatsapp", incoming["customer_name"], incoming["phone"], whatsapp_user)
                ok, send_result = send_whatsapp_text(incoming["phone"], result["reply"], business_id)
                out = {"received": True, "processed": True, "result": result, "reply_sent": ok}
                if not ok:
                    out["reply_error"] = send_result
                else:
                    messages = database.load_messages_by_business(business_id)
                    messages.append({"id": datetime.utcnow().strftime("%Y%m%d%H%M%S%f"), "message_id": incoming.get("message_id", ""), "channel": "whatsapp", "direction": "outbound", "customer_name": incoming["customer_name"], "phone": incoming["phone"], "message": result["reply"], "intent": "ai_reply", "created_at": datetime.utcnow().isoformat() + "Z"})
                    database.save_messages_by_business(business_id, messages)
                self.send_json(200, out); return

            if self.path in ("/chat", "/webhook/message"):
                user=auth_required(self)
                if not user: return
                if not user_can(user,"messages"):
                    self.send_json(403,{"error":"AI müşteri asistanına erişim yetkiniz yok."}); return
                if usage_limit_reached(user,"ai_messages"):
                    self.send_json(429,{"error":"Aylık AI mesaj limitinize ulaştınız.","usage":usage_payload(user[1])}); return
                message = str(data.get("message", "")).strip()
                if not message:
                    self.send_json(400, {"error": "message alanı gerekli"}); return
                result = handle_customer_message(message, str(data.get("channel", "web")).strip() or "web", str(data.get("customer_name", "")).strip(), str(data.get("phone", "")).strip(), user)
                database.increment_usage(user[1],"ai_messages")
                self.send_json(200, result); return

            if self.path == "/api/users/create":
                user=auth_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi çalışan ekleyebilir."}); return
                email=str(data.get("email","")).strip().lower()
                password=str(data.get("password",""))
                role=str(data.get("role","staff")).strip()
                if not email or "@" not in email or len(password) < 8:
                    self.send_json(400,{"error":"Geçerli e-posta ve en az 8 karakterli şifre gerekli."}); return
                if role != "staff":
                    self.send_json(400,{"error":"Yeni çalışan hesapları staff rolüyle oluşturulabilir."}); return
                if database.get_user_by_email(email):
                    self.send_json(409,{"error":"Bu e-posta zaten kayıtlı."}); return
                uid=database.create_user(email,hash_password(password),user[1],role)
                database.write_audit_log(user[1],user[0],"user_created","user",uid,{"email":email,"role":role})
                self.send_json(201,{"id":uid,"email":email,"role":role}); return

            if self.path == "/api/users/delete":
                user=auth_required(self)
                if not user or user[3] != "owner":
                    if user: self.send_json(403,{"error":"Sadece işletme sahibi çalışan silebilir."})
                    return
                target=int(data.get("user_id",0))
                if target == user[0]:
                    self.send_json(400,{"error":"Kendi hesabınızı silemezsiniz."}); return
                target_user=database.get_user_in_business(user[1],target)
                if not target_user:
                    self.send_json(404,{"error":"Çalışan bulunamadı."}); return
                if target_user[2]=="owner" and database.count_owners(user[1])<=1:
                    self.send_json(400,{"error":"Son owner hesabı silinemez."}); return
                database.delete_user(user[1],target)
                database.write_audit_log(user[1],user[0],"user_deleted","user",target,{"email":target_user[1]})
                self.send_json(200,{"success":True}); return

            if self.path == "/api/users/permissions":

                user=auth_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi yetki değiştirebilir."}); return
                target=int(data.get("user_id",0))
                allowed={"appointments","customers","messages","business_settings","team","reports"}
                incoming=data.get("permissions")
                if not isinstance(incoming,dict):
                    self.send_json(400,{"error":"permissions gerekli"}); return
                permissions={k:bool(incoming.get(k,False)) for k in allowed}
                if target == user[0]:
                    self.send_json(400,{"error":"Kendi owner yetkilerinizi bu ekrandan değiştirmeyin."}); return
                target_user=database.get_user_in_business(user[1],target)
                if not target_user:
                    self.send_json(404,{"error":"Çalışan bulunamadı"}); return
                if target_user[2] == "owner":
                    self.send_json(400,{"error":"Owner hesabının yetkileri değiştirilemez."}); return
                if not database.update_user_permissions(user[1],target,permissions):
                    self.send_json(404,{"error":"Çalışan bulunamadı"}); return
                database.write_audit_log(user[1],user[0],"permissions_updated","user",target,{"permissions":permissions})
                self.send_json(200,{"success":True,"permissions":permissions}); return

            if self.path == "/api/users/role":
                user=auth_required(self)
                if not user: return
                if user[3] != "owner":
                    self.send_json(403,{"error":"Sadece işletme sahibi yetki değiştirebilir."}); return
                target=int(data.get("user_id",0)); role=str(data.get("role","staff")).strip()
                if role not in {"owner","staff"}:
                    self.send_json(400,{"error":"Geçersiz rol."}); return
                if target == user[0]:
                    self.send_json(400,{"error":"Kendi rolünüzü bu ekrandan değiştiremezsiniz."}); return
                target_user=database.get_user_in_business(user[1],target)
                if not target_user:
                    self.send_json(404,{"error":"Kullanıcı bulunamadı."}); return
                if target_user[2] == "owner" and role == "staff" and database.count_owners(user[1]) <= 1:
                    self.send_json(400,{"error":"İşletmede en az bir owner kalmalıdır."}); return
                database.update_user_role(user[1],target,role)
                database.write_audit_log(user[1],user[0],"role_updated","user",target,{"role":role})
                self.send_json(200,{"success":True,"role":role}); return

            if self.path == "/api/business":
                user=auth_required(self)
                if not user: return
                if not user_can(user,"business_settings"):
                    self.send_json(403,{"error":"İşletme ayarlarını değiştirme yetkiniz yok."}); return
                name=str(data.get("business_name","")).strip()
                config=data.get("config") if isinstance(data.get("config"),dict) else data
                if name: config["business_name"]=name
                if not str(config.get("business_name","")).strip():
                    self.send_json(400,{"error":"İşletme adı gerekli"}); return
                whatsapp_phone_number_id=data.get("whatsapp_phone_number_id", config.get("whatsapp_phone_number_id"))
                whatsapp_access_token=data.get("whatsapp_access_token")
                if whatsapp_phone_number_id is not None:
                    database.update_business_whatsapp(user[1], whatsapp_phone_number_id=whatsapp_phone_number_id, access_token=whatsapp_access_token)
                config.pop("whatsapp_access_token", None)
                config.pop("whatsapp_phone_number_id", None)
                database.update_business(user[1], str(config["business_name"]), config)
                database.write_audit_log(user[1],user[0],"business_settings_updated","business",user[1],{"business_name":config.get("business_name","")})
                saved=dict(config)
                saved["whatsapp_phone_number_id"]=database.get_business_whatsapp(user[1]).get("phone_number_id","")
                saved["whatsapp_connected"]=bool(database.get_business_whatsapp(user[1]).get("phone_number_id") and database.get_business_whatsapp(user[1]).get("access_token"))
                self.send_json(200, saved); return

            if self.path == "/api/appointments":
                user=auth_required(self)
                if not user: return
                if not user_can(user,"appointments"):
                    self.send_json(403,{"error":"Randevu işlemi yapma yetkiniz yok."}); return
                if usage_limit_reached(user,"appointments"):
                    self.send_json(429,{"error":"Aylık randevu limitinize ulaştınız.","usage":usage_payload(user[1])}); return
                appointment, error = create_appointment(data, user)
                if not appointment:
                    self.send_json(409 if error and "başka bir randevu" in error else 400, {"error": error or "Randevu oluşturulamadı"}); return
                database.increment_usage(user[1],"appointments")
                database.write_audit_log(user[1],user[0],"appointment_created","appointment",appointment.get("id"),{"date":appointment.get("date"),"time":appointment.get("time"),"service":appointment.get("service")})
                if google_calendar:
                    try: google_calendar.sync_appointment(user[1],appointment)
                    except Exception: pass
                self.send_json(201, appointment); return

            if self.path == "/api/appointments/status":
                user=auth_required(self)
                if not user: return
                if not user_can(user,"appointments"):
                    self.send_json(403,{"error":"Randevu durumunu değiştirme yetkiniz yok."}); return
                appointment_id=str(data.get("id","")).strip()
                status=str(data.get("status","")).strip()
                if status not in {"pending","confirmed","cancelled","completed"}:
                    self.send_json(400,{"error":"Geçersiz durum"}); return
                items=load_appointments(user)
                for item in items:
                    if item["id"] == appointment_id:
                        item["status"]=status
                        save_appointments(items,user)
                        if google_calendar:
                            try: google_calendar.sync_appointment(user[1],item)
                            except Exception: pass
                        self.send_json(200,{"success":True}); return
                self.send_json(404,{"error":"Randevu bulunamadı"}); return

            self.send_json(404,{"error":"Not found"})
        except Exception as exc:
            self.send_json(500,{"error":str(exc)})

if __name__ == "__main__":
    initialize_database()
    port=int(os.environ.get("PORT","8080"))
    print(f"AI İşletme Asistanı: http://0.0.0.0:{port}")
    HTTPServer(("0.0.0.0",port),Handler).serve_forever()


