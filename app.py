import os, json, secrets, sqlite3, urllib.parse
from datetime import datetime, timezone
from pathlib import Path
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
DB = os.getenv("DATABASE_PATH", "hr_bridge.db")
app = FastAPI(title="HR Bridge", version="0.1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS applicants(
          id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT UNIQUE,
          name TEXT, email TEXT, phone TEXT, vacancy_id TEXT, vacancy_name TEXT,
          source TEXT, status TEXT DEFAULT 'new', resume_url TEXT,
          bitrix_lead_id TEXT, raw_json TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT, message TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        """)
init_db()

def log(message, level="info"):
    with connect() as c:
        c.execute("INSERT INTO logs(level,message,created_at) VALUES(?,?,?)",
          (level, message, datetime.now(timezone.utc).isoformat()))

def get_setting(key, default=""):
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else os.getenv(key, default)

def set_setting(key, value):
    with connect() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (key, value))

def store_applicant(a):
    with connect() as c:
        c.execute("""INSERT OR IGNORE INTO applicants
          (external_id,name,email,phone,vacancy_id,vacancy_name,source,resume_url,raw_json,created_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (a["external_id"],a.get("name"),a.get("email"),a.get("phone"),a.get("vacancy_id"),
           a.get("vacancy_name"),a.get("source","hh.ru"),a.get("resume_url"),
           json.dumps(a, ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        row = c.execute("SELECT * FROM applicants WHERE external_id=?", (a["external_id"],)).fetchone()
    return dict(row)

def bitrix(method, params):
    hook = get_setting("BITRIX_WEBHOOK_URL").rstrip("/")
    if not hook:
        raise RuntimeError("BITRIX_WEBHOOK_URL не настроен")
    r = requests.post(f"{hook}/{method}.json", json=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data.get("error_description") or data["error"])
    return data.get("result")

def send_to_bitrix(a):
    for kind, value in (("PHONE",a.get("phone")),("EMAIL",a.get("email"))):
        if value:
            found = bitrix("crm.duplicate.findbycomm",
                {"type":kind,"values":[value],"entity_type":"LEAD"})
            if isinstance(found,dict) and found.get("LEAD"):
                return str(found["LEAD"][0])
    fields = {"TITLE":f"Кандидат: {a.get('name') or 'Без имени'}",
      "NAME":a.get("name") or "",
      "SOURCE_ID":"OTHER",
      "SOURCE_DESCRIPTION":f"{a.get('source')} / вакансия {a.get('vacancy_name') or a.get('vacancy_id') or '-'}",
      "COMMENTS":f"Импорт HR Bridge. Резюме: {a.get('resume_url') or 'ссылка недоступна'}"}
    if a.get("phone"): fields["PHONE"]=[{"VALUE":a["phone"],"VALUE_TYPE":"WORK"}]
    if a.get("email"): fields["EMAIL"]=[{"VALUE":a["email"],"VALUE_TYPE":"WORK"}]
    return str(bitrix("crm.lead.add", {"fields":fields}))

@app.get("/", response_class=HTMLResponse)
def home():
    return Path("static/index.html").read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    return {"ok":True,"service":"hr-bridge"}

@app.get("/api/applicants")
def applicants():
    with connect() as c:
        return [dict(x) for x in c.execute("SELECT * FROM applicants ORDER BY id DESC LIMIT 500")]

@app.get("/api/logs")
def logs():
    with connect() as c:
        return [dict(x) for x in c.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 100")]

@app.post("/api/demo/import")
def demo_import():
    demo = [
      {"external_id":"demo-hh-1001","name":"Анна Смирнова","email":"anna@example.com","phone":"+79990001122","vacancy_id":"12345","vacancy_name":"Менеджер по продажам","source":"hh-demo"},
      {"external_id":"demo-hh-1002","name":"Иван Петров","email":"ivan@example.com","phone":"+79990003344","vacancy_id":"12345","vacancy_name":"Менеджер по продажам","source":"hh-demo"}]
    for a in demo: store_applicant(a)
    log(f"Демо-импорт: обработано {len(demo)} кандидата")
    return {"processed":len(demo),"message":"Демо-кандидаты добавлены локально"}

@app.post("/api/applicants/{applicant_id}/send")
def send_one(applicant_id: int):
    with connect() as c:
        row = c.execute("SELECT * FROM applicants WHERE id=?", (applicant_id,)).fetchone()
    if not row: raise HTTPException(404,"Кандидат не найден")
    a = dict(row)
    if a["bitrix_lead_id"]: return {"ok":True,"lead_id":a["bitrix_lead_id"],"duplicate":True}
    try:
        lead_id = send_to_bitrix(a)
        with connect() as c:
            c.execute("UPDATE applicants SET bitrix_lead_id=?,status='sent' WHERE id=?",(lead_id,applicant_id))
        log(f"Кандидат {a.get('name')} отправлен в Bitrix24: лид {lead_id}")
        return {"ok":True,"lead_id":lead_id}
    except Exception as e:
        log(f"Ошибка Bitrix24: {e}","error")
        raise HTTPException(502,str(e))

@app.get("/auth/hh")
def auth_hh():
    client_id, redirect_uri = get_setting("HH_CLIENT_ID"), get_setting("HH_REDIRECT_URI")
    if not client_id or not redirect_uri: raise HTTPException(400,"Настройте HH_CLIENT_ID и HH_REDIRECT_URI")
    state = secrets.token_urlsafe(24)
    set_setting("HH_OAUTH_STATE",state)
    url = "https://hh.ru/oauth/authorize?" + urllib.parse.urlencode({
      "response_type":"code","client_id":client_id,"redirect_uri":redirect_uri,"state":state})
    return RedirectResponse(url)

@app.get("/auth/hh/callback")
def auth_hh_callback(code: str, state: str):
    if not secrets.compare_digest(state,get_setting("HH_OAUTH_STATE")):
        raise HTTPException(400,"OAuth state mismatch")
    r = requests.post("https://hh.ru/oauth/token",data={
      "grant_type":"authorization_code","client_id":get_setting("HH_CLIENT_ID"),
      "client_secret":get_setting("HH_CLIENT_SECRET"),"code":code,
      "redirect_uri":get_setting("HH_REDIRECT_URI")},
      headers={"User-Agent":"HR-Bridge/0.1"},timeout=30)
    r.raise_for_status()
    set_setting("HH_ACCESS_TOKEN",r.json()["access_token"])
    log("HH.ru OAuth подключён")
    return RedirectResponse("/")

@app.post("/api/hh/sync")
def hh_sync(vacancy_id: str):
    token = get_setting("HH_ACCESS_TOKEN")
    if not token: raise HTTPException(400,"Сначала подключите HH.ru")
    headers={"Authorization":f"Bearer {token}","User-Agent":"HR-Bridge/0.1"}
    response=requests.get("https://api.hh.ru/negotiations",
      params={"vacancy_id":vacancy_id,"per_page":50},headers=headers,timeout=30)
    response.raise_for_status()
    payload=response.json()
    imported=0
    for collection in payload.get("collections",[]):
        url=collection.get("url")
        if not url: continue
        next_url=url if url.startswith("http") else "https://api.hh.ru"+url
        while next_url:
            page=requests.get(next_url,headers=headers,timeout=30)
            page.raise_for_status()
            data=page.json()
            for item in data.get("items",[]):
                nid=item.get("id")
                if not nid: continue
                detail=requests.get(f"https://api.hh.ru/negotiations/{nid}",headers=headers,timeout=30)
                if detail.status_code != 200: continue
                d=detail.json()
                person=d.get("applicant") or {}
                resume=d.get("resume") or {}
                name=" ".join(x for x in [person.get("first_name"),person.get("middle_name"),person.get("last_name")] if x) or resume.get("title")
                email=phone=None
                for contact in person.get("contacts") or d.get("contacts") or []:
                    if contact.get("type")=="email": email=contact.get("value")
                    if contact.get("type") in ("cell","phone"): phone=contact.get("value")
                store_applicant({"external_id":f"hh-{nid}","name":name,"email":email,"phone":phone,
                  "vacancy_id":vacancy_id,"vacancy_name":(d.get("vacancy") or {}).get("name"),
                  "source":"hh.ru","resume_url":((resume.get("download") or {}).get("url")),"raw":d})
                imported+=1
            page_no=data.get("page",0)+1
            next_url=(next_url.split("?")[0]+f"?page={page_no}&per_page=50") if page_no < data.get("pages",0) else None
    log(f"HH sync {vacancy_id}: обработано {imported}")
    return {"processed":imported}

@app.post("/api/settings")
async def update_settings(request: Request):
    data=await request.json()
    allowed={"HH_CLIENT_ID","HH_CLIENT_SECRET","HH_REDIRECT_URI","BITRIX_WEBHOOK_URL"}
    for k,v in data.items():
        if k in allowed and v: set_setting(k,str(v))
    return {"ok":True}

@app.get("/api/settings/status")
def settings_status():
    return {k:bool(get_setting(k)) for k in ["HH_CLIENT_ID","HH_CLIENT_SECRET","HH_REDIRECT_URI","BITRIX_WEBHOOK_URL","HH_ACCESS_TOKEN"]}
