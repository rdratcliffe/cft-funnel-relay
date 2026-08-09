"""CFT funnel lead intake: form POST -> GHL contact + tags + note + opportunity + task."""
import os, json, base64, urllib.request

GHL = "https://services.leadconnectorhq.com"
CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json"}

def resp(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body)}

def ghl(path, payload):
    req = urllib.request.Request(GHL + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["GHL_KEY"],
                 "Version": "2021-07-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

def parse(event):
    # fields may arrive merged into event, or as raw __ow_body (possibly base64 JSON)
    if event.get("first_name") or event.get("email") or event.get("phone"):
        return event
    raw = event.get("__ow_body") or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        try:
            return json.loads(base64.b64decode(raw).decode())
        except Exception:
            return {}

def main(event):
    method = (event.get("__ow_method") or event.get("http", {}).get("method") or "post").upper()
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": ""}

    d = parse(event)
    first = (d.get("first_name") or "").strip()
    phone = (d.get("phone") or "").strip()
    email = (d.get("email") or "").strip()
    if not first or not (phone or email):
        return resp(400, {"ok": False, "error": "first_name and phone or email required"})

    loc = os.environ["GHL_LOCATION"]
    tags = ["funnel-lead"]
    if (d.get("sms_consent") or "") == "yes":
        tags.append("sms-consent")
    try:
        payload = {"locationId": loc, "firstName": first,
                   "lastName": (d.get("last_name") or "").strip(),
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
        return resp(200, {"ok": True})
    except Exception as e:
        return resp(200, {"ok": False, "error": str(e)[:200]})
