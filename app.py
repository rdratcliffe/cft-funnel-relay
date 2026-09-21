"""CFT funnel lead relay + Funnel Scorecard dashboard (App Platform web service)."""
import os, json, threading, time as _time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import collector
import janitor as janitor_mod
import net

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

# Tags that end the drip for a contact, whatever step they are on. GHL's opt-out
# workflow adds 'customer replied stop'; Michelle adds 'out of area' by hand.
DRIP_STOP_TAGS = {"no-sms", "duplicate-merge-needed", "customer replied stop",
                  "out of area", "no longer interested", "dnd"}

def _drip_eligible(c, start):
    """(eligible, reason). Pure function of a contact record so it is testable."""
    added = c.get("dateAdded") if isinstance(c.get("dateAdded"), str) else ""
    if added[:10] < start:
        return False, "before DRIP_START"
    tags = [str(t).lower() for t in (c.get("tags") or []) if t]
    stop = DRIP_STOP_TAGS.intersection(tags)
    if stop:
        return False, "stop tag: " + ",".join(sorted(stop))
    if c.get("dnd") is True:
        return False, "dnd"
    dnd_settings = c.get("dndSettings") if isinstance(c.get("dndSettings"), dict) else {}
    sms_setting = dnd_settings.get("SMS") if isinstance(dnd_settings.get("SMS"), dict) else {}
    sms = sms_setting.get("status")
    if sms and str(sms).lower() != "inactive":  # GHL enum: active | inactive | permanent
        return False, "dnd sms"
    if not ("funnel-lead" in tags or "facebook ads" in tags):
        return False, "not ad-sourced"
    return True, ""

def _drip_pass():
    import urllib.parse as _up
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    if (os.environ.get("DRIP_ENABLED") or "").lower() != "true":
        return {"enabled": False}
    dry = (os.environ.get("DRIP_DRY_RUN") or "").lower() == "true"
    ET = _tz(_td(hours=-4))
    now = _dt.now(_tz.utc)
    if not (9 <= now.astimezone(ET).hour < 19):
        return {"enabled": True, "skipped": "outside send window"}
    start = os.environ.get("DRIP_START", "2026-08-13")
    loc = os.environ["GHL_LOCATION"]
    sent, errors, skipped = [], [], {}
    # GHL /contacts/ pagination needs BOTH startAfter (dateAdded ms) AND startAfterId.
    # With startAfterId alone every page is the same first 100 contacts, so each
    # eligible contact was processed 6x per pass and got every step 6x (2026-08-22 ->
    # 2026-09-19, ~1,100 redundant SMS on the paid-lead cohort alone). Use the cursor
    # GHL hands back and dedupe by id so a repeated page can never repeat a send.
    contacts, seen, sa, sa_ts, pages = [], set(), None, None, 0
    for _ in range(6):
        q = {"locationId": loc, "limit": 100}
        if sa and sa_ts: q["startAfterId"] = sa; q["startAfter"] = sa_ts
        try:
            page = ghl_get("/contacts/?" + _up.urlencode(q))
        except Exception as e:
            errors.append({"page": pages, "err": str(e)[:100]}); break
        pages += 1
        batch = [c for c in page.get("contacts", []) if c.get("id") and c["id"] not in seen]
        if not batch: break
        seen.update(c["id"] for c in batch); contacts.extend(batch)
        meta = page.get("meta") or {}
        sa, sa_ts = meta.get("startAfterId"), meta.get("startAfter")
        if not (sa and sa_ts): break
        if (batch[-1].get("dateAdded") or "9999")[:10] < start: break
    for c in contacts:
        try:
            _drip_contact(c, start, now, sent, errors, skipped, dry)
        except Exception as e:  # one malformed record must never abort the whole pass
            errors.append({"contact": (c or {}).get("id"), "err": "record: " + str(e)[:80]})
    dupes = [] if dry else _drip_duplicate_check(sent, now)
    if not dry:
        # Always called, even with no dupes, so the per-scope dedup clears between incidents and a
        # second incident can never be muted by the first (contact ids lead the detail for the same reason).
        _alert([{"level": "critical", "code": "DRIP_DUPLICATE",
                 "detail": "%s: %d contact(s) got the same drip text more than once in this pass" % (", ".join(d["contact"] for d in dupes)[:48], len(dupes))}] if dupes else [], scope="drip")
    return {"enabled": True, "dry_run": dry, "pages": pages, "contacts": len(contacts),
            "sent": sent, "errors": errors, "skipped": skipped, "duplicates": dupes, "ran_at": now.isoformat()}

def _drip_thread(contact_id):
    """All raw messages across a contact's conversations (read-only)."""
    import urllib.parse as _up
    msgs = []
    for cv in ghl_get("/conversations/search?" + _up.urlencode({"locationId": os.environ["GHL_LOCATION"], "contactId": contact_id})).get("conversations") or []:
        m = ghl_get("/conversations/%s/messages?limit=100" % cv["id"]).get("messages", [])
        msgs.extend(m.get("messages", []) if isinstance(m, dict) else (m or []))
    return msgs

def _drip_copies(messages, body, now, minutes=24 * 60):
    """Pure: how many outbound copies of `body` (the text actually sent, name substituted) landed
    in the last `minutes` (None = ever; default 24h — a step is sent once, so a second copy on any pass that day
    is a duplicate; the 30-min window missed once-per-pass repeats). Matches on the first 40
    characters of the real body, so templates that open with the lead's name are matched correctly."""
    from datetime import timedelta as _td
    key = (body or "").strip()[:40]
    if not key:
        return 0
    copies = 0
    for x in messages or []:
        if (x.get("direction") or "").lower() != "outbound":
            continue
        if not (x.get("body") or "").strip().startswith(key):
            continue
        t = net.parse_ts(x.get("dateAdded"))
        if t and (minutes is None or t >= now - _td(minutes=minutes)):
            copies += 1
    return copies

def _drip_duplicate_check(sent, now):
    """Post-send verification: re-read each contact's conversation and count copies of the text
    just sent in the last 24 hours. The 2026-08-22..09-19 six-times bug would have tripped this
    on its first pass; it must never be silent again. Never raises."""
    import urllib.parse as _up
    dupes = []
    for s in sent:
        try:
            copies = _drip_copies(_drip_thread(s["contact"]), s.get("body"), now)
            if copies > 1:
                dupes.append({"contact": s["contact"], "step": s["step"], "copies": copies})
        except Exception:
            continue  # verification must never break the pass; the dashboard shows the sent list regardless
    return dupes

def _drip_contact(c, start, now, sent, errors, skipped, dry):
    from datetime import datetime as _dt
    ok, why = _drip_eligible(c, start)
    if not ok:
        skipped[why] = skipped.get(why, 0) + 1; return
    age_days = (now - _dt.fromisoformat((c.get("dateAdded") or "").replace("Z", "+00:00"))).total_seconds() / 86400
    tags = [str(t).lower() for t in (c.get("tags") or []) if t]
    if True:
        for step in DRIP_STEPS:
            if age_days >= step["day"] and step["tag"] not in tags:
                msg = os.environ.get("DRIP_MSG_" + step["tag"][-1], "")
                if not msg: break
                # The list above is served from a search index that can lag; re-read the
                # contact itself right before sending so a step already sent (or a fresh
                # opt-out) can never be sent again.
                try:
                    fresh = (ghl_get("/contacts/" + c["id"]) or {}).get("contact") or {}
                except Exception as e:
                    errors.append({"contact": c["id"], "err": "refetch: " + str(e)[:80]}); break
                ok2, why2 = _drip_eligible(fresh, start)
                ftags = [str(t).lower() for t in (fresh.get("tags") or []) if t]
                if not ok2 or step["tag"] in ftags:
                    key = "fresh: " + (why2 or "step already sent")
                    skipped[key] = skipped.get(key, 0) + 1
                    break
                first = ((fresh.get("firstName") or "").strip() or (c.get("firstName") or "").strip()).title()
                msg = msg.replace("{name}", first) if first else msg.replace(" {name}", "").replace("{name}", "")
                # Tags can be wiped by anything that upserts the contact. The conversation cannot:
                # if this step's text is already in the thread (last 30 days), it was sent. Re-tag, skip.
                try:
                    if _drip_copies(_drip_thread(c["id"]), msg, now, minutes=None) >= 1:
                        skipped["thread already has step"] = skipped.get("thread already has step", 0) + 1
                        if not dry:
                            try: ghl("/contacts/" + c["id"] + "/tags", {"tags": [step["tag"]]})
                            except Exception: pass
                        break
                except Exception as e:
                    errors.append({"contact": c["id"], "err": "thread check: " + str(e)[:80]}); break
                if dry:
                    sent.append({"contact": c["id"], "step": step["tag"], "dry_run": True}); break
                # Tag BEFORE sending: the tag is the state. If the send then fails, this
                # contact loses one nurture text; the reverse order (send, then tag) turned a
                # failed tag write into a duplicate text on the next pass.
                try:
                    ghl("/contacts/" + c["id"] + "/tags", {"tags": [step["tag"]]})
                except Exception as e:
                    errors.append({"contact": c["id"], "err": "tag: " + str(e)[:90]}); break
                try:
                    ghl("/conversations/messages", {"type": "SMS", "contactId": c["id"], "message": msg})
                    sent.append({"contact": c["id"], "step": step["tag"], "body": msg})
                except Exception as e:
                    errors.append({"contact": c["id"], "step": step["tag"], "err": "send (step tagged, not resent): " + str(e)[:80]})
                break  # max one step per contact per pass (spacing guarantee)

DASH = {"data": None, "ts": 0.0, "lock": threading.Lock(), "running": False, "janitor": None, "drip": None, "net": None, "net_lock": threading.Lock(), "net_running": False, "alerted": {}}

def _alert(alarms, scope="watchdog"):
    """Email the operator on NEW critical/serious alarms (dashboard is pull; this is the push).
    Dedup state is kept per caller (`scope`) so the drip's alarms and the collector's never clobber each other."""
    key = os.environ.get("SENDGRID_API_KEY"); to = os.environ.get("ALERT_EMAIL")
    if not key or not to:
        return
    hot = {f"{a['code']}|{a['detail'][:60]}" for a in alarms if a.get("level") in ("critical", "serious")}
    alerted = DASH.setdefault("alerted", {})
    if not isinstance(alerted, dict):
        alerted = DASH["alerted"] = {}
    new = hot - alerted.get(scope, set())
    if not new:
        alerted[scope] = hot
        return
    lines = [a for a in alarms if a.get("level") in ("critical", "serious")]
    body = "CFT Funnel Watchdog:\n\n" + "\n".join(f"[{a['level'].upper()}] {a['code']}: {a['detail']}" for a in lines) \
           + "\n\nDashboard: https://go.centralfloridatrimlight.com/dash (key in vault)"
    payload = {"personalizations": [{"to": [{"email": to}]}],
               "from": {"email": os.environ.get("ALERT_FROM", "robby@centralfloridatrimlight.com"), "name": "CFT Funnel Watchdog"},
               "subject": f"Funnel alarm: {sorted(a['code'] for a in lines)[0]}" + (f" +{len(lines)-1} more" if len(lines) > 1 else ""),
               "content": [{"type": "text/plain", "value": body}]}
    try:
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send",
                                     data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=30)
        alerted[scope] = hot
        DASH["alert_status"] = {"ok": True, "http": r.status, "at": datetime.now(timezone.utc).isoformat(), "alarms": len(new)}
    except Exception as e:
        # NEVER silent: surface on the dashboard payload; the collector run also re-alerts next pass
        DASH["alert_status"] = {"ok": False, "error": str(e)[:200], "at": datetime.now(timezone.utc).isoformat()}

def _collect_now():
    with DASH["lock"]:
        if DASH["running"]:
            return
        DASH["running"] = True
    try:
        DASH["data"] = collector.collect()
        DASH["ts"] = _time.time()
        _alert(DASH["data"].get("alarms") or [])
    except Exception as e:
        DASH["data"] = {"error": str(e)[:300], "generated_at": datetime.now(timezone.utc).isoformat()}
    finally:
        DASH["running"] = False

def _reconcile_ledger():
    """Safety net for MAPPING_BROKEN: any Meta form submission missing from GHL gets pushed in
    (canonical fields + note + opportunity + CALL task, tagged recovered-lead)."""
    import urllib.parse as _up
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    fb_tok = os.environ.get("FACEBOOK_ACCESS_TOKEN")
    if not fb_tok:
        return {"error": "no fb token"}
    loc = os.environ["GHL_LOCATION"]
    since = (_dt.now(_tz.utc) - _td(days=3)).strftime("%Y-%m-%d")
    def _p10(p):
        d = "".join(c for c in str(p or "") if c.isdigit()); return d[-10:] if len(d) >= 10 else d
    ledger = []
    for form in ("6684499951617295", "1058930670198812", "2250569802393579", "1362395256070979"):
        try:
            d = json.loads(urllib.request.urlopen(
                f"https://graph.facebook.com/v21.0/{form}/leads?fields=created_time,ad_name,field_data&limit=50&access_token={fb_tok}",
                timeout=45).read())
        except Exception:
            continue
        for l in d.get("data", []):
            if l["created_time"][:10] >= since:
                fd = {f["name"]: f.get("values", [""])[0] for f in l.get("field_data", [])}
                ledger.append({"created": l["created_time"], "ad": l.get("ad_name"), **fd})
    # Known phones/emails from the newest contacts. Paging needs BOTH startAfter and startAfterId
    # (the drip had the same bug: startAfterId alone returns the same first 100 five times, so any
    # lead older than the newest 100 looked "missing" and was re-recovered every 30 minutes —
    # 2026-09-19..21: 155 duplicate tasks/notes on two contacts, and the upsert wiped their drip
    # tags so the drip re-sent step 1 every pass until one of them replied STOP).
    phones, emails, seen = set(), set(), set()
    sa, sa_ts = None, None
    for _ in range(6):
        q = {"locationId": loc, "limit": 100}
        if sa and sa_ts: q["startAfterId"] = sa; q["startAfter"] = sa_ts
        try:
            page = ghl_get("/contacts/?" + _up.urlencode(q))
        except Exception:
            break
        batch = [c for c in page.get("contacts", []) if c.get("id") and c["id"] not in seen]
        if not batch: break
        seen.update(c["id"] for c in batch)
        phones |= {_p10(c.get("phone")) for c in batch if c.get("phone")}
        emails |= {(c.get("email") or "").strip().lower() for c in batch if c.get("email")}
        meta = page.get("meta") or {}
        sa, sa_ts = meta.get("startAfterId"), meta.get("startAfter")
        if not (sa and sa_ts): break
        if (batch[-1].get("dateAdded") or "9999")[:10] < since: break
    stats = {"lookups": 0, "lookup_errors": 0, "last_lookup_error": None, "existed": 0}
    def _exists_in_ghl(ph, em):
        """Direct lookup by phone, then email. Recovery happens ONLY when GHL itself says nobody
        has this phone/email — the paged list above is an optimisation, never the verdict.
        A failed lookup counts as 'exists' (never create on uncertainty) AND is counted, so a dead
        search endpoint shows on the dashboard instead of silently disabling recovery."""
        for q in ([ph] if ph else []) + ([em] if em else []):
            stats["lookups"] += 1
            try:
                hits = ghl("/contacts/search", {"locationId": loc, "pageLimit": 3, "query": q}).get("contacts") or []
                if any(_p10(h.get("phone")) == ph or (em and (h.get("email") or "").strip().lower() == em) for h in hits):
                    return True
            except Exception as e:
                stats["lookup_errors"] += 1; stats["last_lookup_error"] = str(e)[:120]
                return True
        return False
    recovered = []
    for l in ledger:
        ph = _p10(l.get("phone_number")); em = (l.get("email") or "").strip().lower()
        if not ph or ph in phones or (em and em in emails): continue
        if _exists_in_ghl(ph, em): phones.add(ph); stats["existed"] += 1; continue
        name = (l.get("full_name") or "").split(" ", 1)
        first, last = name[0] or "Lead", (name[1] if len(name) > 1 else "")
        interest = l.get("what_are_you_lighting") or "not specified"
        try:
            c = ghl("/contacts/upsert", {"locationId": loc, "firstName": first, "lastName": last,
                    "phone": "+1" + ph, "email": l.get("email") or None,
                    "address1": l.get("street_address") or "", "city": l.get("city") or "",
                    "postalCode": l.get("zip_code") or "", "source": "Facebook Ads"})
            cid = c["contact"]["id"]
            if c.get("new") is False:
                # GHL matched an existing contact after all: no note / opportunity / task, or the
                # 2026-09-19..21 duplicate storm repeats. The search gate above is belt; this is braces.
                phones.add(ph); stats["existed"] += 1
                recovered.append({"name": (first + " " + last).strip(), "skipped": "existed at upsert"})
                continue
            # Tags are ADDED, never passed to upsert: GHL upsert replaces the whole tag list.
            try: ghl("/contacts/" + cid + "/tags", {"tags": ["facebook ads", "recovered-lead"]})
            except Exception: pass
            note = f"AUTO-RECOVERED (form->GHL sync gap)\nAd: {l.get('ad')}\nSubmitted: {l['created']}\nInterest: {interest}"
            for fn in (lambda: ghl("/contacts/" + cid + "/notes", {"body": note}),
                       lambda: ghl("/opportunities/", {"locationId": loc, "pipelineId": os.environ["PIPELINE_ID"],
                            "pipelineStageId": os.environ["STAGE_ID"], "contactId": cid, "status": "open",
                            "name": (first + " " + last).strip() + " - " + interest}),
                       lambda: ghl("/contacts/" + cid + "/tasks", {"title": "CALL RECOVERED LEAD NOW: " + first + " +1" + ph,
                            "body": note, "dueDate": "2099-01-01T00:00:00Z", "completed": False})):
                try: fn()
                except Exception: pass
            phones.add(ph)
            recovered.append({"name": (first + " " + last).strip(), "submitted": l["created"]})
        except Exception as e:
            recovered.append({"error": str(e)[:80]})
    if stats["lookup_errors"]:
        _alert([{"level": "serious", "code": "RECONCILER_LOOKUP_FAILED",
                 "detail": "%d of %d GHL contact lookups failed (%s); recovery is fail-closed until this clears" % (stats["lookup_errors"], stats["lookups"], stats["last_lookup_error"])}], scope="reconciler")
    else:
        _alert([], scope="reconciler")
    return {"recovered": recovered, "ledger_checked": len(ledger), **stats}

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
        try:
            DASH["reconciler"] = _reconcile_ledger()
        except Exception as e:
            DASH["reconciler"] = {"error": str(e)[:200]}
        _time.sleep(1800)

def _net_run(send=True):
    """Compute the Net and (optionally) email it. Runs at boot and at 9:00 / 13:00 / 17:00 ET Mon-Sat.
    Stateless; nothing to drift. Never raises: a failure lands on the dashboard as an error result."""
    with DASH["net_lock"]:
        if DASH["net_running"]:
            return DASH.get("net")
        DASH["net_running"] = True
    try:
        try:
            result = net.compute()
        except Exception as e:
            result = {"error": str(e)[:300], "generated_at": datetime.now(timezone.utc).isoformat(),
                      "untouched_leads": [], "waiting_on_reply": [], "errors": [str(e)[:200]]}
        if send:
            try:
                result["delivery"] = net.send_email(result)
            except Exception as e:
                result["delivery"] = {"ok": False, "error": "send: " + str(e)[:200]}
        elif isinstance(DASH.get("net"), dict) and DASH["net"].get("delivery"):
            result["delivery"] = DASH["net"]["delivery"]  # keep the last real delivery receipt visible
        DASH["net"] = result
        return result
    finally:
        DASH["net_running"] = False

def _net_loop():
    if (os.environ.get("NET_ENABLED") or "true").lower() != "true":
        return
    # Boot: always compute (warm dashboard, catch the backlog); only EMAIL when a person would be
    # reading it — business hours and not within 15 min of a scheduled slot (avoids a double).
    now = datetime.now(timezone.utc)
    et_hour = now.astimezone(net.ET).hour
    boot_send = 8 <= et_hour < 20 and now.astimezone(net.ET).weekday() < 6 and (net.next_slot(now) - now).total_seconds() > 15 * 60
    _net_run(send=boot_send)
    while True:
        target = net.next_slot()
        _time.sleep(max(60, (target - datetime.now(timezone.utc)).total_seconds()))
        _net_run()

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

def ghl_get(path):
    req = urllib.request.Request(GHL + path,
        headers={"Authorization": "Bearer " + os.environ["GHL_KEY"], "Version": "2021-07-28",
                 "User-Agent": "cft-funnel-relay/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)

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
                   "source": (d.get("utm_source") or "").strip() or "Funnel"}
        if phone: payload["phone"] = phone
        if email: payload["email"] = email
        c = ghl("/contacts/upsert", payload)
        cid = c["contact"]["id"]
        # Tags are ADDED after the upsert, never passed to it: GHL upsert replaces the whole tag
        # list, so a returning lead re-submitting the form would lose drip / opt-out state.
        try: ghl("/contacts/" + cid + "/tags", {"tags": tags})
        except Exception: pass
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
                             "janitor": DASH["janitor"], "drip": DASH["drip"], "reconciler": DASH.get("reconciler"), "alert_status": DASH.get("alert_status"),
                             "net": {k: (len(v) if isinstance(v, list) else v) for k, v in (DASH.get("net") or {}).items() if k in ("generated_at", "untouched_leads", "waiting_on_reply", "delivery", "errors")},
                             "report": DASH["data"]})
            return
        if parsed.path in ("/dash/net", "/net"):  # ingress only routes /dash* and /funnel* to this service
            if not key_ok:
                self._send(401, {"ok": False, "error": "key required"}); return
            want_send = qs.get("send", ["0"])[0] == "1"
            if want_send or qs.get("refresh", ["0"])[0] == "1" or DASH.get("net") is None:
                # A run takes ~2 min of GHL reads; never block the single-threaded server (the
                # funnel lead endpoint lives here). Kick it off and report; poll without refresh.
                if not DASH["net_running"]:
                    threading.Thread(target=_net_run, kwargs={"send": want_send}, daemon=True).start()
                self._send(202, {"ok": True, "running": True, "send": want_send, "net": DASH.get("net")})
                return
            self._send(200, {"ok": True, "running": DASH["net_running"], "net": DASH.get("net")})
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
    threading.Thread(target=_net_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), H).serve_forever()
