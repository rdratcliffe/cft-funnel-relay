"""CFT funnel lead relay - tiny HTTP service (App Platform web service)."""
import os, json, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), H).serve_forever()
