"""CFT funnel lead intake: form POST -> GHL contact + tags + note + opportunity + task."""
import os, json, urllib.request

GHL = "https://services.leadconnectorhq.com"

def ghl(path, payload):
    req = urllib.request.Request(GHL + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + os.environ["GHL_KEY"],
                 "Version": "2021-07-28", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

CORS = {"Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json"}

def resp(code, body):
    return {"statusCode": code, "headers": CORS, "body": json.dumps(body)}

def main(event):
    method = event.get("http", {}).get("method", "POST").upper()
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": CORS, "body": "{}"}

    first = (event.get("first_name") or "").strip()
    phone = (event.get("phone") or "").strip()
    email = (event.get("email") or "").strip()
    if not first or not (phone or email):
        return resp(400, {"ok": False, "error": "first_name and phone or email required"})

    loc = os.environ["GHL_LOCATION"]
    tags = ["funnel-lead"]
    if (event.get("sms_consent") or "") == "yes":
        tags.append("sms-consent")
    try:
        c = ghl("/contacts/upsert", {
            "locationId": loc, "firstName": first,
            "lastName": (event.get("last_name") or "").strip(),
            "phone": phone or None, "email": email or None,
            "postalCode": (event.get("zip") or "").strip(),
            "tags": tags,
            "source": (event.get("utm_source") or "").strip() or "Funnel"})
        cid = c["contact"]["id"]
        interest = (event.get("interest") or "not specified")
        note = ("Funnel lead from " + (event.get("page_source") or "funnel page") +
                "\nInterest: " + interest +
                "\nSMS consent: " + (event.get("sms_consent") or "no") +
                "\nUTM: source=" + (event.get("utm_source") or "-") +
                " medium=" + (event.get("utm_medium") or "-") +
                " campaign=" + (event.get("utm_campaign") or "-") +
                " content=" + (event.get("utm_content") or "-"))
        try:
            ghl("/contacts/" + cid + "/notes", {"body": note})
        except Exception:
            pass
        try:
            ghl("/opportunities/", {
                "locationId": loc, "pipelineId": os.environ["PIPELINE_ID"],
                "pipelineStageId": os.environ["STAGE_ID"], "contactId": cid,
                "status": "open",
                "name": (first + " " + (event.get("last_name") or "")).strip() + " - " + interest})
        except Exception:
            pass
        try:
            ghl("/contacts/" + cid + "/tasks", {
                "title": "CALL NEW FUNNEL LEAD within 5 min: " + first + " " + (phone or email),
                "body": note, "dueDate": "2099-01-01T00:00:00Z", "completed": False})
        except Exception:
            pass
        return resp(200, {"ok": True})
    except Exception as e:
        return resp(200, {"ok": False, "error": str(e)[:200]})
