"""Funnel Scorecard collector — computes every dashboard signal from its SYSTEM OF RECORD,
stateless, per the Signal Contract (business-brain 09-Systems/Signal Contract.md).
No caches, no snapshots: every run rebuilds the full since-epoch series from source APIs.
stdlib only."""
import os, json, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

EPOCH = "2026-08-10"                      # relaunch day
HCP_KILL_UTC = "2026-08-12T18:00:00Z"     # MSR key deleted; leads must never precede estimates in HCP after this
ET = timezone(timedelta(hours=-4))        # America/New_York (DST); revisit at Nov clock change
BIZ_START, BIZ_END = 8, 20                # business hours ET
FB = "https://graph.facebook.com/v21.0"
GHL = "https://services.leadconnectorhq.com"
HCP = "https://api.housecallpro.com"
UA = "cft-funnel-relay/1.0"

FORMS = ["6684499951617295", "1058930670198812", "2250569802393579"]  # all form generations
CAMP_A = "CFT Relaunch A"
CAMP_B = "CFT Relaunch B"
# Canonical creative binding per Campaign A ad (SCHEMA_DRIFT check): the creative IS the form binding.
AD_ROSTER = {
    "120251280305080695": "1358352846475450",   # Ad 1 - Tiktok style
    "120251280305890695": "1066904132963548",   # Ad 2 - Corporate
    "120251289209260695": "1037611695921089",   # A2 - Short Video 1
    "120251289238340695": "1336820654859824",   # A2 - Jason House
    "120251289302240695": "4465283527086295",   # A3 - Lee Testimonial
    "120251289358900695": "1471763474843716",   # A3 - Short Video 2
}

def _get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def _safe(fn, default):
    try:
        return fn()
    except Exception as e:
        return {"_error": str(e)[:200]} if default is None else default

def _phone10(p):
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else d

def collect():
    fb_tok = os.environ["FACEBOOK_ACCESS_TOKEN"]
    ghl_h = {"Authorization": "Bearer " + os.environ["GHL_KEY"], "Version": "2021-07-28"}
    hcp_h = {"Authorization": "Token " + os.environ["HCP_API_KEY"]}
    loc = os.environ["GHL_LOCATION"]
    now = datetime.now(timezone.utc)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    alarms = []

    def alarm(level, code, detail):
        alarms.append({"level": level, "code": code, "detail": detail})

    # ---- 1. Meta: spend by campaign by day (system of record: billing insights) ----
    daily = {}
    ins = _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
        "level": "campaign", "time_increment": 1,
        "time_range": json.dumps({"since": EPOCH, "until": today}),
        "fields": "campaign_name,spend,impressions,clicks,ctr,actions", "limit": 200,
        "access_token": fb_tok})), {"data": []})
    for r in ins.get("data", []):
        d, cname = r["date_start"], r.get("campaign_name", "")
        key = "A" if CAMP_A in cname else ("B" if CAMP_B in cname else "old")
        day = daily.setdefault(d, {})
        cur = day.setdefault(key, {"spend": 0.0, "impr": 0, "clicks": 0, "lpv": 0})
        cur["spend"] += float(r.get("spend", 0)); cur["impr"] += int(r.get("impressions", 0))
        cur["clicks"] += int(r.get("clicks", 0))
        for a in r.get("actions", []) or []:
            if a["action_type"] == "landing_page_view":
                cur["lpv"] += int(float(a["value"]))

    # ---- 2. Lead ledger (system of record for instant-form leads: Meta form submissions) ----
    ledger = []
    for form in FORMS:
        url = f"{FB}/{form}/leads?fields=created_time,ad_name,ad_id&limit=100&access_token={fb_tok}"
        while url:
            page = _safe(lambda: _get(url), {"data": []})
            for l in page.get("data", []):
                if l["created_time"][:10] >= EPOCH:
                    ledger.append({"created": l["created_time"], "ad": l.get("ad_name", "?"), "form": form})
            url = page.get("paging", {}).get("next")
            if page.get("data") and page["data"][-1]["created_time"][:10] < EPOCH:
                break
    ledger_by_day = {}
    for l in ledger:
        ledger_by_day[l["created"][:10]] = ledger_by_day.get(l["created"][:10], 0) + 1

    # ---- 3. GHL lead cohort (contacts since epoch) + first outbound CALL per lead ----
    contacts, start_after = [], None
    for _ in range(8):
        q = {"locationId": loc, "limit": 100}
        if start_after: q["startAfterId"] = start_after
        page = _safe(lambda: _get(f"{GHL}/contacts/?" + urllib.parse.urlencode(q), ghl_h), {"contacts": []})
        batch = page.get("contacts", [])
        if not batch: break
        contacts.extend(batch)
        start_after = batch[-1].get("id")
        if batch[-1].get("dateAdded", "9999")[:10] < EPOCH: break
    seen_ids = set()
    leads = []
    for c in contacts:
        if c.get("id") in seen_ids: continue
        seen_ids.add(c.get("id"))
        if (c.get("dateAdded") or "")[:10] < EPOCH: continue
        tags = [t.lower() for t in (c.get("tags") or [])]
        src = (c.get("source") or "").lower()
        if "funnel-lead" in tags: source = "funnel_page"
        elif "facebook" in src or "facebook ads" in tags: source = "instant_form"
        else: source = "other"
        leads.append({"id": c["id"], "name": (f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip() or "(no name)"),
                      "phone10": _phone10(c.get("phone")), "created": c.get("dateAdded"),
                      "source": source, "first_call_min": None, "booked": False, "ran": False,
                      "no_show_risk": False, "won": False, "revenue": 0.0})
    # first outbound call per lead (mechanism: TYPE_CALL outbound in the conversation)
    for l in leads:
        convs = _safe(lambda: _get(f"{GHL}/conversations/search?locationId={loc}&contactId={l['id']}", ghl_h), {})
        calls = []
        for cv in convs.get("conversations", []) or []:
            msgs = _safe(lambda: _get(f"{GHL}/conversations/{cv['id']}/messages", ghl_h), {})
            for m in (msgs.get("messages", {}) or {}).get("messages", []) or []:
                if m.get("messageType") == "TYPE_CALL" and m.get("direction") == "outbound":
                    calls.append(m.get("dateAdded"))
        if calls and l["created"]:
            t0 = datetime.fromisoformat(l["created"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(min(calls).replace("Z", "+00:00"))
            l["first_call_min"] = round((t1 - t0).total_seconds() / 60, 1)

    # ---- 4. HCP: estimates (booked mechanism) + jobs (won/revenue mechanism) ----
    cust_phone = {}
    def hcp_cust_phone(cid):
        if cid not in cust_phone:
            c = _safe(lambda: _get(f"{HCP}/customers/{cid}", hcp_h), {})
            cust_phone[cid] = _phone10(c.get("mobile_number") or c.get("home_number") or c.get("work_number"))
        return cust_phone[cid]
    estimates = _safe(lambda: _get(f"{HCP}/estimates?page_size=100&sort_by=created_at&sort_direction=desc", hcp_h), {}).get("estimates", [])
    jobs = _safe(lambda: _get(f"{HCP}/jobs?page_size=100&sort_by=created_at&sort_direction=desc", hcp_h), {}).get("jobs", [])
    by_phone = {l["phone10"]: l for l in leads if l["phone10"]}
    for e in estimates:
        if (e.get("created_at") or "")[:10] < EPOCH: continue
        ph = hcp_cust_phone((e.get("customer") or {}).get("id"))
        l = by_phone.get(ph)
        if not l: continue
        l["booked"] = True
        ws = (e.get("work_status") or "").lower()
        sched = ((e.get("schedule") or {}).get("scheduled_start") or "")
        if ws and ws != "scheduled": l["ran"] = True
        elif sched and sched < now.strftime("%Y-%m-%dT%H:%M:%SZ"): l["no_show_risk"] = True
    for j in jobs:
        if (j.get("created_at") or "")[:10] < EPOCH: continue
        ph = hcp_cust_phone((j.get("customer") or {}).get("id"))
        l = by_phone.get(ph)
        if not l: continue
        l["won"] = True
        amt = j.get("total_amount") or 0
        l["revenue"] += (amt / 100.0) if amt > 100000 else float(amt)

    # ---- 5. Watchdog checks (each per Signal Contract) ----
    # 5a. MAPPING_BROKEN: ledger vs GHL instant-form leads per day
    ghl_if_by_day = {}
    for l in leads:
        if l["source"] == "instant_form":
            d = l["created"][:10]; ghl_if_by_day[d] = ghl_if_by_day.get(d, 0) + 1
    for d, n in ledger_by_day.items():
        if d < today and ghl_if_by_day.get(d, 0) < n:
            alarm("critical", "MAPPING_BROKEN", f"{d}: {n} form submissions but only {ghl_if_by_day.get(d,0)} reached GHL")
    # 5b. CPL_ALARM: trailing 3 full days, per campaign group
    last3 = sorted([d for d in daily if d < today])[-3:]
    sp3 = sum(daily[d].get("A", {}).get("spend", 0) for d in last3)
    ld3 = sum(ledger_by_day.get(d, 0) for d in last3)
    if sp3 > 100 and (ld3 == 0 or sp3 / max(ld3, 1) > 50):
        alarm("serious", "CPL_ALARM", f"Campaign A trailing-3d CPL ${sp3/max(ld3,1):.0f} (${sp3:.0f}/{ld3} leads)")
    # 5c. ZERO_LEAD day
    for d in last3:
        if daily.get(d, {}).get("A", {}).get("spend", 0) > 50 and ledger_by_day.get(d, 0) == 0:
            alarm("serious", "ZERO_LEAD", f"{d}: ${daily[d]['A']['spend']:.0f} spent, zero form submissions")
    # 5d. AD_STALLED: active A ad with $0 on last full day
    yday = (now.astimezone(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    ad_ins = _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
        "level": "ad", "time_range": json.dumps({"since": yday, "until": yday}),
        "fields": "ad_id,spend", "limit": 100, "access_token": fb_tok})), {"data": []})
    spent_ids = {r.get("ad_id") for r in ad_ins.get("data", []) if float(r.get("spend", 0)) > 0}
    for ad_id in AD_ROSTER:
        st = _safe(lambda: _get(f"{FB}/{ad_id}?fields=effective_status,creative{{id}}&access_token={fb_tok}"), {})
        if st.get("effective_status") == "ACTIVE" and ad_id not in spent_ids and daily.get(yday):
            alarm("warning", "AD_STALLED", f"ad {ad_id} ACTIVE but $0 spend on {yday}")
        # 5e. SCHEMA_DRIFT: creative binding changed
        cr = (st.get("creative") or {}).get("id")
        if cr and cr != AD_ROSTER[ad_id]:
            alarm("warning", "SCHEMA_DRIFT", f"ad {ad_id} creative changed to {cr} (expected {AD_ROSTER[ad_id]})")
    # 5f. HCP_PUSH_RESURRECTED: post-kill HCP customer matching a lead phone, no estimate
    est_phones = {hcp_cust_phone((e.get("customer") or {}).get("id")) for e in estimates}
    hcust = _safe(lambda: _get(f"{HCP}/customers?page_size=50&sort_by=created_at&sort_direction=desc", hcp_h), {}).get("customers", [])
    for c in hcust:
        if (c.get("created_at") or "") <= HCP_KILL_UTC: continue
        ph = _phone10(c.get("mobile_number"))
        if ph and ph in by_phone and ph not in est_phones:
            alarm("critical", "HCP_PUSH_RESURRECTED", f"lead {by_phone[ph]['name']} appeared in HCP with no estimate")
    # 5g. SLA breaches (business-hours leads)
    sla_breaches, sla_samples = 0, []
    for l in leads:
        if l["source"] not in ("instant_form", "funnel_page"):
            continue  # SLA covers ad-response leads; voice-AI/manual/synced contacts have their own flows
        t0 = datetime.fromisoformat(l["created"].replace("Z", "+00:00")).astimezone(ET)
        in_biz = BIZ_START <= t0.hour < BIZ_END
        if l["first_call_min"] is not None:
            if in_biz:
                sla_samples.append(l["first_call_min"])
                if l["first_call_min"] > 15: sla_breaches += 1
        elif in_biz and (now - t0.astimezone(timezone.utc)).total_seconds() > 7200:
            sla_breaches += 1
            alarm("serious", "SLA_BREACH", f"{l['name']} ({l['source']}) uncalled for >2h")
    # 5h. INFRA: page marker
    try:
        page_html = urllib.request.urlopen("https://go.centralfloridatrimlight.com/", timeout=20).read().decode()
        if "What are you interested in having done?" not in page_html:
            alarm("critical", "INFRA_DOWN", "funnel page serving but canonical form marker missing")
    except Exception as e:
        alarm("critical", "INFRA_DOWN", f"funnel page unreachable: {str(e)[:80]}")
    if not alarms:
        alarm("good", "ALL_CLEAR", "every watchdog check passed")

    # ---- 6. Assemble ----
    def funnel_row(sel):
        n = len(sel); booked = sum(1 for l in sel if l["booked"]); ran = sum(1 for l in sel if l["ran"])
        won = sum(1 for l in sel if l["won"]); contacted = sum(1 for l in sel if l["first_call_min"] is not None)
        return {"leads": n, "contacted": contacted, "booked": booked, "ran": ran,
                "no_show_risk": sum(1 for l in sel if l["no_show_risk"]),
                "won": won, "revenue": round(sum(l["revenue"] for l in sel), 2)}
    spend_a = sum(v.get("A", {}).get("spend", 0) for v in daily.values())
    spend_b = sum(v.get("B", {}).get("spend", 0) for v in daily.values())
    funnel = {"instant_form": funnel_row([l for l in leads if l["source"] == "instant_form"]) | {"spend": round(spend_a, 2)},
              "funnel_page": funnel_row([l for l in leads if l["source"] == "funnel_page"]) | {"spend": round(spend_b, 2)},
              "other": funnel_row([l for l in leads if l["source"] == "other"]) | {"spend": 0},
              "total": funnel_row(leads) | {"spend": round(spend_a + spend_b, 2)}}
    sla_samples.sort()
    return {
        "generated_at": now.isoformat(), "epoch": EPOCH,
        "daily": [{"date": d,
                   "A": {k: round(v, 2) if isinstance(v, float) else v for k, v in daily[d].get("A", {}).items()},
                   "B": {k: round(v, 2) if isinstance(v, float) else v for k, v in daily[d].get("B", {}).items()},
                   "old": {k: round(v, 2) if isinstance(v, float) else v for k, v in daily[d].get("old", {}).items()},
                   "ledger_leads": ledger_by_day.get(d, 0)} for d in sorted(daily)],
        "funnel": funnel,
        "sla": {"median_min": sla_samples[len(sla_samples)//2] if sla_samples else None,
                "breaches": sla_breaches, "calls_measured": len(sla_samples)},
        "leads": sorted(leads, key=lambda l: l["created"], reverse=True),
        "alarms": alarms,
    }
