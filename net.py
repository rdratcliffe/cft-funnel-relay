"""The Net — who is falling through the cracks right now, read from GHL (system of record).

Two lists, computed stateless on every run (Signal Contract: mechanism, not proxy):
  A. Ad-sourced leads with NO human touch after >2 business hours (Mon-Sat 8am-8pm ET).
     Human = an outbound SMS/email/call carrying a GHL userId. Workflow/app automation is not a touch.
  B. The lead spoke last and no human has replied for >60 min (last 14 days).
     Missed inbound calls count; answered ones do not. A short closer ("sounds good") within
     6h of a human message is the end of an exchange, not a gap; the same words after a robot
     mean a person answered automation and is still waiting.

Opt-outs (customer replied stop / out of area / no longer interested / DND) are excluded from both.
Delivery: plain-text email via SendGrid to NET_TO (comma list; defaults to ALERT_EMAIL).
Stdlib only, like the rest of this service.
"""
import json, os, re, time, urllib.error, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

GHL = "https://services.leadconnectorhq.com"
ET = timezone(timedelta(hours=-4))  # same fixed offset as app.py (documented DST caveat)
STOP_TAGS = {"customer replied stop", "out of area", "no-sms", "no longer interested", "dnd", "duplicate-merge-needed"}
STOP_WORDS = re.compile(r"^\s*(stop|out|unsubscribe|cancel|quit|no thanks|not interested)\W*$", re.I)
MISSED_CALL = {"no-answer", "voicemail", "missed", "busy", "canceled", "cancelled"}
AD_SOURCE_KEYS = ("facebook", "funnel-lead", "meta_ads", "ig", "fb")
UNTOUCHED_AFTER_BUSINESS_HOURS = 2.0
UNANSWERED_AFTER_MINUTES = 60
LOOKBACK_LEADS_DAYS = 7
LOOKBACK_CONVERSATIONS_DAYS = 14
CONVERSATIONS_TO_SCAN = 100
CONVERSATION_PAGES = 5  # 500 most recent conversations; the 14-day cut stops paging earlier


# ── pure helpers (unit-tested) ─────────────────────────────────────────────
def parse_ts(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000.0, timezone.utc)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", str(s).replace("Z", "+00:00"))
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def business_hours_between(a, b):
    """Business hours (Mon-Sat 8am-8pm ET) elapsed between a and b, in 15-minute steps."""
    h, t = 0.0, a
    while t < b:
        te = t.astimezone(ET)
        nxt = min(b, t + timedelta(minutes=15))
        if te.weekday() < 6 and 8 <= te.hour < 20:
            h += (nxt - t).total_seconds() / 3600
        t = nxt
    return h


def is_ad_sourced(contact):
    s = ((contact.get("source") or "") + " " + " ".join(str(t) for t in (contact.get("tags") or []) if t)).lower()
    return any(k in s for k in AD_SOURCE_KEYS)


def is_opted_out(contact):
    tags = {str(t).lower() for t in (contact.get("tags") or []) if t}
    return bool(tags & STOP_TAGS) or contact.get("dnd") is True


def normalize_messages(raw, users):
    """GHL message dicts -> sorted list of {t, dir, human, who, type, call_status, body}."""
    out = []
    for x in raw or []:
        mt = (x.get("messageType") or "").upper()
        if mt.startswith("TYPE_ACTIVITY"):
            continue
        t = parse_ts(x.get("dateAdded") or x.get("dateUpdated"))
        if not t:
            continue
        d = (x.get("direction") or "").lower()
        human = bool(x.get("userId")) and d == "outbound"
        meta = x.get("meta") if isinstance(x.get("meta"), dict) else {}
        call = meta.get("call") if isinstance(meta.get("call"), dict) else {}
        out.append({"t": t, "dir": d, "human": human,
                    "who": users.get(x.get("userId"), "rep") if human else ("lead" if d == "inbound" else "auto"),
                    "type": mt.replace("TYPE_", ""),
                    "call_status": str(call.get("status") or "").lower(),
                    "body": (x.get("body") or "").replace("\n", " ").strip()})
    out.sort(key=lambda m: m["t"])
    return out


REACTION = re.compile(r"^\s*(removed an? \w+ from|liked|loved|laughed at|emphasized|disliked|questioned)\s+[\u201c\"]", re.I)  # iMessage tapbacks
INTERNAL_NOTE = re.compile(r"^\s*(\[[A-Z][A-Z _-]+\]|Sophia is transferring|Transfer to you)")  # Sophia's [PROPOSAL] / [NEW LEAD] / transfer notes, not a homeowner
CONFIRMED = re.compile(r"^\s*confirmed\b", re.I)  # reply to an appointment reminder
REMINDER_ECHO = re.compile(r"^\s*(hey|hi)\s+\w+,?\s+(your estimate (with central florida trimlight )?(has been|is in)|this text is to confirm|your appointment with central florida)", re.I)  # our own reminder text echoed back
EMOJI_ONLY = re.compile(r"^[\s\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2764\uFE0F\U0001F1E6-\U0001F1FF\U0001F3FB-\U0001F3FF!.]+$")
SHORT_WORDS = 6
YES_NO = re.compile(r"^\s*(yes|yeah|yep|yup|no|nope|sure|absolutely|not yet)\W*$", re.I)


def _is_noise(m):
    """Inbound messages that are not a person asking for anything."""
    body = m["body"] or ""
    if m["type"] == "CALL":
        return m["call_status"] not in MISSED_CALL  # answered call = conversation; missed = keep
    if not body:
        return False  # empty SMS = attachment/photo; keep, a person sent something
    return bool(STOP_WORDS.match(body) or INTERNAL_NOTE.match(body) or REACTION.match(body)
                or REMINDER_ECHO.match(body) or EMOJI_ONLY.match(body) or CONFIRMED.match(body))


def waiting_on_human(ms, now):
    """List-B rule. Returns the inbound message the lead is waiting on, or None.

    The 'tail' is everything the lead said after the last human outbound. Noise (tapbacks, emoji,
    reminder echoes, STOP, answered calls) is dropped. A substantive tail message (a question, or
    more than SHORT_WORDS words, or a missed call, or an attachment) is a gap. A short reply is a gap
    only when it answers a question: a robot's question, or a human's question more than 6 h earlier
    (a short reply within 6 h of a human is a live exchange, not a gap)."""
    last_human_t = max([m["t"] for m in ms if m["human"]], default=None)
    tail = [m for m in ms if m["dir"] == "inbound" and (last_human_t is None or m["t"] > last_human_t)]
    tail = [m for m in tail if not _is_noise(m) and (now - m["t"]).total_seconds() <= LOOKBACK_CONVERSATIONS_DAYS * 86400]
    if not tail:
        return None  # nothing in the window (automation may have kept the thread 'recent')
    if (now - tail[-1]["t"]).total_seconds() < UNANSWERED_AFTER_MINUTES * 60:
        return None  # give the human an hour
    if last_human_t is not None and (tail[-1]["t"] - last_human_t).total_seconds() < 30 * 60 \
            and not any("?" in (m["body"] or "") or m["type"] == "CALL" for m in tail):
        return None  # everything the lead said came within 30 min of a human: a live exchange, not a gap (unless they asked, or called and missed)
    substantive = [m for m in tail if m["type"] == "CALL" or not m["body"] or "?" in m["body"] or len(m["body"].split()) > SHORT_WORDS]
    if substantive:
        return next((m for m in substantive if "?" in (m["body"] or "")), substantive[0])
    last = tail[-1]
    prev_out = [m for m in ms if m["dir"] == "outbound" and m["t"] < last["t"]]
    prev_human = [m for m in prev_out if m["human"]]
    human_recent = bool(prev_human) and (last["t"] - prev_human[-1]["t"]).total_seconds() < 6 * 3600
    if human_recent:
        return None
    answered = bool(YES_NO.match(last["body"] or ""))  # a bare yes/no is a person answering something
    asked = bool(prev_out) and "?" in (prev_out[-1]["body"] or "")
    asked_by_human = bool(prev_human) and "?" in (prev_human[-1]["body"] or "") and (last["t"] - prev_human[-1]["t"]).total_seconds() < 24 * 3600
    if answered or asked or asked_by_human:
        return last
    return None


def _last_outbound_was_automation(ms, last):
    """True when the message the lead replied to was sent by automation, not a person."""
    prev_out = [m for m in ms if m["dir"] == "outbound" and m["t"] < last["t"]]
    return bool(prev_out) and not prev_out[-1]["human"]


# ── GHL access ─────────────────────────────────────────────────────────────
def _req(path, version, payload=None):
    """One GHL call with a single retry on transient failures (401/429/5xx/network): a 5-minute
    scan makes ~800 calls and GHL throws the occasional spurious 401."""
    data = json.dumps(payload).encode() if payload is not None else None
    last_err = None
    for attempt in range(2):
        req = urllib.request.Request(GHL + path, data=data, headers={
            "Authorization": "Bearer " + os.environ["GHL_KEY"], "Version": version,
            "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "cft-funnel-relay/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (401, 429, 500, 502, 503, 504) or attempt:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt:
                raise
        time.sleep(2.0)
    raise last_err


def _messages_for_contact(contact_id, users, fetch=_req):
    convs = fetch("/conversations/search?" + urllib.parse.urlencode({"locationId": os.environ["GHL_LOCATION"], "contactId": contact_id}), "2021-04-15").get("conversations") or []
    raw, conv_id = [], None
    for cv in convs:
        conv_id = conv_id or cv.get("id")
        m = fetch("/conversations/%s/messages?limit=100" % cv["id"], "2021-04-15").get("messages", [])
        raw.extend(m.get("messages", []) if isinstance(m, dict) else (m or []))
        time.sleep(0.15)
    return normalize_messages(raw, users), conv_id


def compute(now=None, fetch=_req):
    now = now or datetime.now(timezone.utc)
    loc = os.environ["GHL_LOCATION"]
    errors = []
    users = {}
    try:
        users = {u["id"]: (u.get("name") or ("%s %s" % (u.get("firstName", ""), u.get("lastName", ""))).strip())
                 for u in fetch("/users/?" + urllib.parse.urlencode({"locationId": loc}), "2021-07-28").get("users", []) if u.get("id")}
    except Exception as e:
        errors.append("users: " + str(e)[:120])

    # A. ad-sourced leads, last 7 days, no human touch
    A, contacts, sa = [], [], None
    since = now - timedelta(days=LOOKBACK_LEADS_DAYS)
    try:
        for _ in range(10):
            body = {"locationId": loc, "pageLimit": 100,
                    "filters": [{"field": "dateAdded", "operator": "range",
                                 "value": {"gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "lte": now.strftime("%Y-%m-%dT%H:%M:%SZ")}}],
                    "sort": [{"field": "dateAdded", "direction": "desc"}]}
            if sa:
                body["searchAfter"] = sa
            d = fetch("/contacts/search", "2021-07-28", body)
            batch = d.get("contacts") or []
            if not batch:
                break
            contacts.extend(batch)
            sa = batch[-1].get("searchAfter")
            if not sa or len(contacts) >= (d.get("total") or 0):
                break
    except Exception as e:
        errors.append("contacts: " + str(e)[:120])
    seen_contact_ids = set()
    for c in contacts:
        try:
            if not is_ad_sourced(c) or is_opted_out(c):
                continue
            created = parse_ts(c.get("dateAdded"))
            if not created:
                continue
            age_bh = business_hours_between(created, now)
            if age_bh < UNTOUCHED_AFTER_BUSINESS_HOURS:
                continue
            ms, conv_id = _messages_for_contact(c["id"], users, fetch)
            if any(m["human"] for m in ms):
                continue
            inbound = [m for m in ms if m["dir"] == "inbound"]
            seen_contact_ids.add(c["id"])
            A.append({"contact_id": c["id"], "name": ("%s %s" % (c.get("firstName") or "", c.get("lastName") or "")).strip() or "(no name)",
                      "phone": c.get("phone") or "", "source": c.get("source") or "", "created": created.isoformat(),
                      "business_hours_waiting": round(age_bh, 1), "auto_messages": len([m for m in ms if m["who"] == "auto"]),
                      "replied": (inbound[-1]["body"][:80] or "[%s]" % inbound[-1]["type"]) if inbound else "", "conversation_id": conv_id})
        except Exception as e:
            errors.append("lead %s: %s" % (c.get("id"), str(e)[:100]))

    # B. lead spoke last, no human reply
    B, convs = [], []
    cut = now - timedelta(days=LOOKBACK_CONVERSATIONS_DAYS)
    try:
        start_after = None
        for _ in range(CONVERSATION_PAGES):
            q = {"locationId": loc, "limit": CONVERSATIONS_TO_SCAN, "sort": "desc", "sortBy": "last_message_date"}
            if start_after:
                q["startAfterDate"] = start_after
            page = fetch("/conversations/search?" + urllib.parse.urlencode(q), "2021-04-15").get("conversations") or []
            if not page:
                break
            convs.extend(page)
            last_ts = parse_ts(page[-1].get("lastMessageDate"))
            raw_last = page[-1].get("lastMessageDate")
            if not last_ts or last_ts < cut or len(page) < CONVERSATIONS_TO_SCAN or not raw_last or raw_last == start_after:
                break
            start_after = raw_last
    except Exception as e:
        errors.append("conversations: " + str(e)[:120])
    for cv in convs:
        try:
            lmd = parse_ts(cv.get("lastMessageDate"))
            if not lmd or lmd < cut or not cv.get("contactId"):
                continue
            # lastMessageDirection is NOT trusted: automation often speaks after the lead and hides the gap.
            ms, conv_id = _messages_for_contact(cv["contactId"], users, fetch)
            last = waiting_on_human(ms, now)
            if not last:
                continue
            contact = fetch("/contacts/" + cv["contactId"], "2021-07-28").get("contact") or {}
            time.sleep(0.15)
            if is_opted_out(contact):
                continue
            name = cv.get("fullName") or cv.get("contactName") or ("%s %s" % (contact.get("firstName") or "", contact.get("lastName") or "")).strip() or "(no name)"
            if name in {n.strip() for n in (os.environ.get("NET_IGNORE_NAMES") or "Robby Ratcliffe,(863) 450-1704").split(",") if n.strip()}:
                continue  # operator / test contacts
            last_human = [m for m in ms if m["human"]]
            B.append({"contact_id": cv["contactId"], "name": name, "phone": contact.get("phone") or cv.get("phone") or "",
                      "when": last["t"].isoformat(), "said": last["body"][:120] or ("[missed call]" if last["type"] == "CALL" else "[attachment / empty text]"),
                      "after_automation": _last_outbound_was_automation(ms, last),
                      "last_human": (last_human[-1]["t"].isoformat() + " " + last_human[-1]["who"]) if last_human else "never",
                      "conversation_id": conv_id or cv.get("id")})
        except Exception as e:
            errors.append("conv %s: %s" % (cv.get("id"), str(e)[:100]))
    A.sort(key=lambda r: r["created"])
    B.sort(key=lambda r: r["when"])
    return {"generated_at": now.isoformat(), "untouched_leads": A, "waiting_on_reply": B, "errors": errors,
            "scanned": {"leads_last_7d": len(contacts), "conversations": len(convs)}}


# ── rendering + delivery ───────────────────────────────────────────────────
def _fmt(iso):
    t = parse_ts(iso)
    return t.astimezone(ET).strftime("%a %m/%d %I:%M %p") if t else "?"


def _link(contact_id):
    return "https://app.gohighlevel.com/v2/location/%s/contacts/detail/%s" % (os.environ.get("GHL_LOCATION", ""), contact_id)


def render_text(result):
    A, B = result.get("untouched_leads") or [], result.get("waiting_on_reply") or []
    lines = ["THE NET — %s" % _fmt(result.get("generated_at")), ""]
    if result.get("error"):
        lines += ["⚠ THE NET COULD NOT RUN: %s" % result["error"], "Nothing below is trustworthy; the next scheduled run will retry.", ""]
    lines.append("%d paid lead(s) nobody has personally touched yet (over 2 business hours):" % len(A) if A else "Every paid lead from the last 7 days has had a human touch.")
    for a in A:
        lines.append("  • %s  %s  came in %s  (%s business hrs ago)%s" % (
            a["name"], a["phone"], _fmt(a["created"]), a["business_hours_waiting"],
            ("  — REPLIED: \"%s\"" % a["replied"]) if a["replied"] else ""))
        lines.append("      " + _link(a["contact_id"]))
    lines.append("")
    lines.append("%d person(s) spoke last and are waiting on a human reply (over 60 min):" % len(B) if B else "Nobody is waiting on a reply.")
    for b in B:
        lines.append("  • %s  %s  said %s: \"%s\"%s" % (b["name"], b["phone"], _fmt(b["when"]), b["said"],
                                                       "  (answered a robot, no human yet)" if b["after_automation"] else ""))
        lh = b.get("last_human") or "never"
        lines.append("      last human: %s" % ("never" if lh == "never" else (_fmt(lh.split(" ")[0]) + " " + (lh.split(" ", 1)[1] if " " in lh else "")).strip()))
        lines.append("      " + _link(b["contact_id"]))
    if result.get("errors"):
        lines += ["", "⚠ %d lookup error(s) — some people may be missing from this list:" % len(result["errors"])] + ["  " + e for e in result["errors"][:5]]
    lines += ["", "Rules: human = a message sent by a person in GHL; automation does not count. Opt-outs and out-of-area are excluded.",
              "Sent by cft-funnel-relay /dash/net."]
    return "\n".join(lines)


def subject(result):
    A, B = len(result.get("untouched_leads") or []), len(result.get("waiting_on_reply") or [])
    errs = len(result.get("errors") or []) + (1 if result.get("error") else 0)
    when = _fmt(result.get("generated_at"))
    if errs and not A and not B:
        return "Net %s: ERROR — %d lookup error(s), list NOT computed" % (when, errs)
    base = "Net %s: clear" % when if not A and not B else \
        "Net %s: %d untouched lead%s · %d waiting on a reply" % (when, A, "" if A == 1 else "s", B)
    return ("⚠ " + base + " (%d lookup error(s))" % errs) if errs else base


def recipients():
    raw = os.environ.get("NET_TO") or os.environ.get("ALERT_EMAIL") or ""
    return [e.strip() for e in raw.split(",") if e.strip()]


def send_email(result, to=None):
    key = os.environ.get("SENDGRID_API_KEY")
    to = to or recipients()
    if not key or not to:
        return {"ok": False, "error": "SENDGRID_API_KEY or recipients missing", "to": to}
    try:
        payload = {"personalizations": [{"to": [{"email": e} for e in to]}],
                   "from": {"email": os.environ.get("ALERT_FROM", "robby@centralfloridatrimlight.com"), "name": "CFT Net"},
                   "subject": subject(result), "content": [{"type": "text/plain", "value": render_text(result)}]}
        req = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"ok": True, "http": r.status, "to": to, "at": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "to": to, "at": datetime.now(timezone.utc).isoformat()}


def next_slot(now=None):
    """Next scheduled run: 9:00, 13:00, 17:00 ET, Mon-Sat."""
    now = now or datetime.now(timezone.utc)
    t = now.astimezone(ET)
    for day in range(0, 8):
        d = (t + timedelta(days=day)).replace(second=0, microsecond=0)
        if d.weekday() == 6:
            continue
        for hour in (9, 13, 17):
            cand = d.replace(hour=hour, minute=0)
            if cand > t:
                return cand.astimezone(timezone.utc)
    return (t + timedelta(days=1)).astimezone(timezone.utc)
