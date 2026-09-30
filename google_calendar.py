import json
import os
import secrets
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
import database

GOOGLE_AUTH_URL="https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL="https://oauth2.googleapis.com/token"
CALENDAR_API="https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPE="https://www.googleapis.com/auth/calendar.events"

def configured():
    return bool(os.environ.get("GOOGLE_CLIENT_ID","").strip() and os.environ.get("GOOGLE_CLIENT_SECRET","").strip())

def redirect_uri():
    return os.environ.get("GOOGLE_REDIRECT_URI","").strip() or ((os.environ.get("PUBLIC_APP_URL","").strip().rstrip("/") or "")+"/oauth/google/callback")

def authorization_url(business_id,user_id):
    if not configured(): raise RuntimeError("Google Calendar OAuth yapılandırılmalı.")
    state=secrets.token_urlsafe(32)
    database.create_google_oauth_state(state,business_id,user_id,datetime.now(timezone.utc)+timedelta(minutes=10))
    p={"client_id":os.environ["GOOGLE_CLIENT_ID"].strip(),"redirect_uri":redirect_uri(),"response_type":"code","scope":CALENDAR_SCOPE,"access_type":"offline","prompt":"consent","include_granted_scopes":"true","state":state}
    return GOOGLE_AUTH_URL+"?"+urllib.parse.urlencode(p)

def exchange_code(code):
    body=urllib.parse.urlencode({"code":code,"client_id":os.environ["GOOGLE_CLIENT_ID"].strip(),"client_secret":os.environ["GOOGLE_CLIENT_SECRET"].strip(),"redirect_uri":redirect_uri(),"grant_type":"authorization_code"}).encode()
    req=urllib.request.Request(GOOGLE_TOKEN_URL,data=body,headers={"Content-Type":"application/x-www-form-urlencoded"},method="POST")
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def refresh_access_token(refresh_token):
    body=urllib.parse.urlencode({"client_id":os.environ["GOOGLE_CLIENT_ID"].strip(),"client_secret":os.environ["GOOGLE_CLIENT_SECRET"].strip(),"refresh_token":refresh_token,"grant_type":"refresh_token"}).encode()
    req=urllib.request.Request(GOOGLE_TOKEN_URL,data=body,headers={"Content-Type":"application/x-www-form-urlencoded"},method="POST")
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def _api(method,path,token,payload=None):
    data=json.dumps(payload).encode() if payload is not None else None
    h={"Authorization":"Bearer "+token,"Accept":"application/json"}
    if payload is not None: h["Content-Type"]="application/json"
    req=urllib.request.Request(CALENDAR_API+path,data=data,headers=h,method=method)
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

def access_token_for_business(business_id):
    c=database.get_google_calendar_connection(business_id)
    if not c: return None
    exp=c.get("access_token_expires_at")
    if c.get("access_token") and exp:
        try:
            if datetime.fromisoformat(exp)>datetime.now(timezone.utc)+timedelta(minutes=1): return c["access_token"]
        except ValueError: pass
    if not c.get("refresh_token"): return c.get("access_token")
    t=refresh_access_token(c["refresh_token"])
    access=t.get("access_token")
    if not access: raise RuntimeError("Google erişim tokenı yenilenemedi.")
    database.update_google_calendar_tokens(business_id,access,datetime.now(timezone.utc)+timedelta(seconds=int(t.get("expires_in",3600))))
    return access

def connect_from_callback(state,code):
    oauth=database.consume_google_oauth_state(state)
    if not oauth: raise RuntimeError("Google OAuth oturumu geçersiz veya süresi dolmuş.")
    t=exchange_code(code)
    access=str(t.get("access_token","")).strip()
    refresh=str(t.get("refresh_token","")).strip()
    if not access: raise RuntimeError("Google erişim tokenı alınamadı.")
    if not refresh: refresh=(database.get_google_calendar_connection(oauth["business_id"]) or {}).get("refresh_token","")
    if not refresh: raise RuntimeError("Google refresh token alınamadı. Bağlantıyı yeniden yetkilendirin.")
    info=_api("GET","/users/me/calendarList/primary",access)
    database.save_google_calendar_connection(oauth["business_id"],oauth["user_id"],info.get("id","primary"),info.get("summaryOverride") or info.get("summary") or "Google Calendar",access,refresh,datetime.now(timezone.utc)+timedelta(seconds=int(t.get("expires_in",3600))),t.get("scope",""))
    return oauth["business_id"]

def status(business_id):
    c=database.get_google_calendar_connection(business_id)
    return {"connected":bool(c),"calendar_id":c["calendar_id"] if c else "","calendar_name":c["calendar_name"] if c else ""}

def disconnect(business_id): database.delete_google_calendar_connection(business_id)

def event_payload(a,config):
    tz=str(config.get("timezone","Europe/Istanbul")); start=a["date"]+"T"+a["time"]+":00"
    try: end=datetime.fromisoformat(start)+timedelta(minutes=int(config.get("appointment_duration_minutes",60) or 60))
    except Exception: end=datetime.fromisoformat(start)+timedelta(minutes=60)
    return {"summary":(a["service"]+" - "+a["customer_name"])[:200],"description":(a.get("note","") or "")[:2000],"location":str(config.get("address",""))[:500],"start":{"dateTime":start,"timeZone":tz},"end":{"dateTime":end.strftime("%Y-%m-%dT%H:%M:%S"),"timeZone":tz},"extendedProperties":{"private":{"nexora_business_id":str(a.get("_business_id","")),"nexora_appointment_id":str(a["id"])}}}

def sync_appointment(business_id,a):
    c=database.get_google_calendar_connection(business_id)
    if not c: return None
    token=access_token_for_business(business_id); a=dict(a); a["_business_id"]=business_id
    eid=database.get_google_event_id(a["id"],business_id); cid=urllib.parse.quote(c["calendar_id"],safe="")
    if a.get("status")=="cancelled":
        if eid:
            try: _api("DELETE","/calendars/"+cid+"/events/"+urllib.parse.quote(eid,safe=""),token)
            except urllib.error.HTTPError as e:
                if e.code!=404: raise
            database.delete_google_event(a["id"],business_id)
        return None
    path="/calendars/"+cid+"/events"
    if eid: return _api("PATCH",path+"/"+urllib.parse.quote(eid,safe=""),token,event_payload(a,database.get_business_config(business_id)))
    result=_api("POST",path,token,event_payload(a,database.get_business_config(business_id)))
    database.save_google_event(a["id"],business_id,result.get("id","")); return result

def sync_all(business_id,appointments):
    out=[]
    for a in appointments:
        try:
            r=sync_appointment(business_id,a); out.append({"id":a["id"],"ok":True,"event_id":r.get("id") if r else None})
        except Exception as e: out.append({"id":a["id"],"ok":False,"error":str(e)})
    return out
