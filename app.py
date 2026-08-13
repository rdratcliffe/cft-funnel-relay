"""CFT funnel lead relay + Funnel Scorecard dashboard (App Platform web service)."""
import os, json, threading, time as _time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import collector
import janitor as janitor_mod

# ---- Lead-only nurture drip (GHL SMS; PRIVATE channel — no Meta Ad Library exposure) ----
# ENABLED only when env DRIP_ENABLED=true (operator gate: copy approved 1:1 before enable).
# Eligibility: ad-sourced leads (funnel-lead / facebook ads tag), created after DRIP_START,
# no 'no-sms' tag. Steps tracked as GHL tags (stateless). Sends only 9am-7pm ET.
DRIP_STEPS = [
    {"day": 1, "tag": "drip-lls-1", "msg": "MSG1"},
    {"day": 2, "tag": "drip-lls-2", "msg": "MSG2"},
    {"day": 4, "tag": "drip-lls-3", "msg": "MSG3"},
    {"day": 6, "tag": "drip-lls-4", "msg": "MSG4"},
    {"day": 8, "tag": "drip-lls-5", "msg": "MSG5"},
]
DRIP_MSGS = {}  # filled from env DRIP_MSG_1..5 so copy changes never need a code deploy

def _drip_pass():
    import urllib.parse as _up
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    if (os.environ.get("DRIP_ENABLED") or "").lower() != "true":
        return {"enabled": False}
    ET = _tz(_td(hours=-4))
    now = _dt.now(_tz.utc)
    if not (9 <= now.astimezone(ET).hour < 19):
        return {"enabled": True, "skipped": "outside send window"}
    start = os.environ.get("DRIP_START", "2026-08-13")
    loc = os.environ["GHL_LOCATION"]
    sent, errors = [], []
    contacts, sa = [], None
    for _ in range(6):
        q = {"locationId": loc, "limit": 100}
        if sa: q["startAfterId"] = sa
        try:
            page = json.loads(urllib.request.urlopen(urllib.request.Request(
                GHL + "/contacts/?" + _up.urlencode(q),
                headers={"Authorization": "Bearer " + os.environ["GHL_KEY"], "Version": "2021-07-28",
                         "User-Agent": "cft-funnel-relay/1.0"}), timeout=45).read())
        except Exception:
            break
        batch = page.get("contacts", [])
        if not batch: break
        contacts.extend(batch); sa = batch[-1].get("id")
        if batch[-1].get("dateAdded", "9999")[:10] < start: break
    for c in contacts:
        added = c.get("dateAdded") or ""
        if added[:10] < start: continue
        tags = [t.lower() for t in (c.get("tags") or [])]
        if "no-sms" in tags or "duplicate-merge-needed" in tags: continue
        if not ("funnel-lead" in tags or "facebook ads" in tags): continue
        try:
            age_days = (now - _dt.fromisoformat(added.replace("Z", "+00:00"))).total_seconds() / 86400
        except Exception:
            continue
        for step in DRIP_STEPS:
            if age_days >= step["day"] and step["tag"] not in tags:
                msg = os.environ.get("DRIP_MSG_" + step["tag"][-1], "")
                if not msg: break
                try:
                    ghl("/conversations/messages", {"type": "SMS", "contactId": c["id"], "message": msg})
                    ghl("/contacts/" + c["id"] + "/tags", {"tags": [step["tag"]]})
                    sent.append({"contact": c["id"], "step": step["tag"]})
                except Exception as e:
                    errors.append({"contact": c["id"], "err": str(e)[:100]})
                break  # max one step per contact per pass (spacing guarantee)
    return {"enabled": True, "sent": sent, "errors": errors, "ran_at": now.isoformat()}


DASH = {"data": None, "ts": 0.0, "lock": threading.Lock(), "running": False, "janitor": None, "drip": None}

def _collect_now():
    with DASH["lock"]:
        if DASH["running"]:
            return
        DASH["running"] = True
    try:
        DASH["data"] = collector.collect()
        DASH["ts"] = _time.time()
    except Exception as e:
        DASH["data"] = {"error": str(e)[:300], "generated_at": datetime.now(timezone.utc).isoformat()}
    finally:
        DASH["running"] = False

def _janitor_loop():
    while True:
        try:
            DASH["janitor"] = janitor_mod.run()
        except Exception as e:
            DASH["janitor"] = {"error": str(e)[:200], "ran_at": datetime.now(timezone.utc).isoformat()}
        try:
            DASH["drip"] = _drip_pass()
        except Exception as e:
            DASH["drip"] = {"error": str(e)[:200]}
        _time.sleep(1800)

def _daily_loop():
    _collect_now()
    while True:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=9, minute=30, second=0, microsecond=0)  # 05:30 ET daily
        if target <= now:
            target = target + timedelta(days=1)
        _time.sleep(max(60, (target - now).total_seconds()))
        _collect_now()

GHL = "https://services.leadconnectorhq.com"
CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json"}

def ghl(path, payload):
    req = urllib.request.Request(GHL + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["GHL_KEY"],
                 "Version": "2021-07-28", "Content-Type": "application/json",
                 "User-Agent": "cft-funnel-relay/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def process(d):
    first = (d.get("first_name") or "").strip()
    phone = (d.get("phone") or "").strip()
    email = (d.get("email") or "").strip()
    if not first or not (phone or email):
        return 400, {"ok": False, "error": "first_name and phone or email required"}
    loc = os.environ["GHL_LOCATION"]
    tags = ["funnel-lead"]
    if (d.get("sms_consent") or "") == "yes":
        tags.append("sms-consent")
    try:
        payload = {"locationId": loc, "firstName": first,
                   "lastName": (d.get("last_name") or "").strip(),
                   "address1": (d.get("street_address") or "").strip(),
                   "city": (d.get("city") or "").strip(),
                   "postalCode": (d.get("zip") or "").strip(),
                   "tags": tags,
                   "source": (d.get("utm_source") or "").strip() or "Funnel"}
        if phone: payload["phone"] = phone
        if email: payload["email"] = email
        c = ghl("/contacts/upsert", payload)
        cid = c["contact"]["id"]
        interest = d.get("interest") or "not specified"
        note = ("Funnel lead from " + (d.get("page_source") or "funnel page") +
                "\nInterest: " + interest +
                "\nSMS consent: " + (d.get("sms_consent") or "no") +
                "\nUTM: source=" + (d.get("utm_source") or "-") +
                " medium=" + (d.get("utm_medium") or "-") +
                " campaign=" + (d.get("utm_campaign") or "-") +
                " content=" + (d.get("utm_content") or "-"))
        for call in (
            lambda: ghl("/contacts/" + cid + "/notes", {"body": note}),
            lambda: ghl("/opportunities/", {
                "locationId": loc, "pipelineId": os.environ["PIPELINE_ID"],
                "pipelineStageId": os.environ["STAGE_ID"], "contactId": cid,
                "status": "open",
                "name": (first + " " + (d.get("last_name") or "")).strip() + " - " + interest}),
            lambda: ghl("/contacts/" + cid + "/tasks", {
                "title": "CALL NEW FUNNEL LEAD within 5 min: " + first + " " + (phone or email),
                "body": note, "dueDate": "2099-01-01T00:00:00Z", "completed": False}),
        ):
            try: call()
            except Exception: pass
        return 200, {"ok": True}
    except Exception as e:
        return 200, {"ok": False, "error": str(e)[:200]}

class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        for k, v in CORS.items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in CORS.items(): self.send_header(k, v)
        self.end_headers()
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        key_ok = qs.get("key", [""])[0] == os.environ.get("DASH_KEY", "") and os.environ.get("DASH_KEY")
        if parsed.path == "/dash":
            if not key_ok:
                self._send(401, {"ok": False, "error": "key required"}); return
            try:
                with open(os.path.join(os.path.dirname(__file__), "dash.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)[:200]})
            return
        if parsed.path == "/dash/data":
            if not key_ok:
                self._send(401, {"ok": False, "error": "key required"}); return
            if qs.get("refresh", ["0"])[0] == "1" or DASH["data"] is None:
                _collect_now()
            self._send(200, {"ok": True, "stale_seconds": int(_time.time() - DASH["ts"]) if DASH["ts"] else None,
                             "janitor": DASH["janitor"], "drip": DASH["drip"], "report": DASH["data"]})
            return
        self._send(200, {"ok": True, "service": "cft-funnel-relay"})
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            d = {}
        code, body = process(d)
        self._send(code, body)
    def log_message(self, *a): pass

if __name__ == "__main__":
    threading.Thread(target=_daily_loop, daemon=True).start()
    threading.Thread(target=_janitor_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), H).serve_forever()
