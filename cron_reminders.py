import os
import urllib.request

url = os.environ["NEXORA_REMINDER_URL"].rstrip("/") + "/internal/reminders"
secret = os.environ["NEXORA_REMINDER_CRON_SECRET"]
req = urllib.request.Request(
    url,
    data=b"{}",
    headers={
        "Content-Type": "application/json",
        "X-Nexora-Cron-Secret": secret,
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=120) as response:
    print(response.read().decode("utf-8"))
