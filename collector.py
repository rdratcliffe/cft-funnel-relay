"""Funnel Scorecard collector v3 — every signal from its VERIFIED mechanism (Signal Contract).
Node-audit findings applied 2026-08-12:
- HCP money is CENTS (verified: Anne Chow job 444000 = $4,440 w/ $2,220 balance). Always /100.
- GHL holds duplicate contact rows per person (phone-touch automations create one per event,
  tag 'name via lookup'); the LEAD ENTITY is the phone-resolved person, not the contact row.
- No-name phone-only rows are touch artifacts, never leads: merged into their entity when the
  phone matches, else listed as capture defects (unnamed_inbound), excluded from lead counts.
- WON = ALL HCP jobs in period (not cohort-filtered), minus canceled; attribution via HCP
  customer.lead_source + ad-cohort phone match as a flag.
- Estimate lifecycle: scheduled_start = booked; work_status 'created job from estimate' = converted.
stdlib only. Stateless recompute."""
import os, json, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

SIGNAL_CONTRACT_VERSION = "1.0.0"   # bump ONLY with a matching edit to vault 09-Systems/Signal Contract.md
EPOCH = "2026-08-10"
HCP_KILL_UTC = "2026-08-12T18:00:00Z"
ET = timezone(timedelta(hours=-4))        # America/New_York (DST); revisit at Nov clock change
BIZ_START, BIZ_END = 8, 20
FB = "https://graph.facebook.com/v21.0"
GHL = "https://services.leadconnectorhq.com"
HCP = "https://api.housecallpro.com"
UA = "cft-funnel-relay/1.0"

FORMS = ["6684499951617295", "1058930670198812", "2250569802393579"]
CAMP_A = "CFT Relaunch A"
CAMP_B = "CFT Relaunch B"
AD_ROSTER = {
    "120251280305080695": "1358352846475450",
    "120251280305890695": "1066904132963548",
    "120251289209260695": "1037611695921089",
    "120251289238340695": "1336820654859824",
    "120251289302240695": "4465283527086295",
    "120251289358900695": "1471763474843716",
}

def _get(url, headers=None, timeout=45):
    req = urllib.request.Request(url, headers={**(headers or {}), "User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default

def _phone10(p):
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else d

def _dollars(cents):
    return round((cents or 0) / 100.0, 2)   # VERIFIED: HCP amounts are cents

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

    # ---- 1. Meta spend/day (mechanism: billing insights) ----
    daily = {}
    ins = _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
        "level": "campaign", "time_increment": 1,
        "time_range": json.dumps({"since": EPOCH, "until": today}),
        "fields": "campaign_name,spend,impressions,clicks,actions", "limit": 200,
        "access_token": fb_tok})), {"data": []})
    for r in ins.get("data", []):
        key = "A" if CAMP_A in r.get("campaign_name", "") else ("B" if CAMP_B in r.get("campaign_name", "") else "old")
        cur = daily.setdefault(r["date_start"], {}).setdefault(key, {"spend": 0.0, "impr": 0, "clicks": 0, "lpv": 0})
        cur["spend"] += float(r.get("spend", 0)); cur["impr"] += int(r.get("impressions", 0))
        cur["clicks"] += int(r.get("clicks", 0))
        for a in r.get("actions", []) or []:
            if a["action_type"] == "landing_page_view":
                cur["lpv"] += int(float(a["value"]))

    # ---- 2. Lead ledger (mechanism: Meta form submissions) ----
    ledger = []
    for form in FORMS:
        url = f"{FB}/{form}/leads?fields=created_time,ad_name&limit=100&access_token={fb_tok}"
        while url:
            page = _safe(lambda: _get(url), {"data": []})
            for l in page.get("data", []):
                if l["created_time"][:10] >= EPOCH:
                    ledger.append({"created": l["created_time"], "ad": l.get("ad_name", "?")})
            url = page.get("paging", {}).get("next")
            if page.get("data") and page["data"][-1]["created_time"][:10] < EPOCH:
                break
    ledger_by_day = {}
    for l in ledger:
        ledger_by_day[l["created"][:10]] = ledger_by_day.get(l["created"][:10], 0) + 1

    # ---- 3. GHL contacts -> PERSON ENTITIES (mechanism: phone-resolved identity) ----
    contacts, start_after = [], None
    for _ in range(10):
        q = {"locationId": loc, "limit": 100}
        if start_after: q["startAfterId"] = start_after
        page = _safe(lambda: _get(f"{GHL}/contacts/?" + urllib.parse.urlencode(q), ghl_h), {"contacts": []})
        batch = page.get("contacts", [])
        if not batch: break
        contacts.extend(batch)
        start_after = batch[-1].get("id")
        if batch[-1].get("dateAdded", "9999")[:10] < EPOCH: break
    rows = [c for c in {c["id"]: c for c in contacts}.values()
            if (c.get("dateAdded") or "")[:10] >= EPOCH]
    entities, unnamed_inbound, dupes = {}, [], {}
    for c in sorted(rows, key=lambda c: c.get("dateAdded") or ""):
        ph = _phone10(c.get("phone"))
        named = bool(c.get("firstName") or c.get("lastName"))
        tags = [t.lower() for t in (c.get("tags") or [])]
        src = (c.get("source") or "").lower()
        if "funnel-lead" in tags: source = "funnel_page"
        elif "facebook" in src or "facebook ads" in tags: source = "instant_form"
        else: source = "other"
        key = ph or c["id"]
        if key in entities:
            e = entities[key]
            e["contact_rows"].append(c["id"])
            if named and e["name"] == "(unnamed)":
                e["name"] = f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip()
            if source != "other" and e["source"] == "other":
                e["source"] = source
            dupes[key] = dupes.get(key, 1) + 1
        else:
            if not named and not ph:
                continue  # nothing resolvable
            if not named:
                # phone-only artifact with no prior entity in cohort: capture defect, not a lead
                unnamed_inbound.append({"contact_id": c["id"], "phone10": ph, "created": c.get("dateAdded"),
                                        "source_field": c.get("source")})
                entities[key] = {"placeholder": True, "contact_rows": [c["id"]], "name": "(unnamed)",
                                 "phone10": ph, "created": c.get("dateAdded"), "source": source}
                continue
            entities[key] = {"contact_rows": [c["id"]], "name": f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip(),
                             "phone10": ph, "created": c.get("dateAdded"), "source": source,
                             "first_call_min": None, "booked": False, "est_status": None,
                             "won": False, "revenue": 0.0}
    # placeholders that later got a named row were upgraded in-place; drop pure placeholders from leads
    leads = [e for e in entities.values() if not e.get("placeholder")]
    for k, n in dupes.items():
        ent = entities[k]
        if n >= 2 and not ent.get("placeholder"):
            alarm("warning", "GHL_DUPLICATE_CONTACT",
                  f"{ent['name']} has {n} GHL contact rows (phone …{k[-4:]}) — merge in GHL; find the automation creating per-touch contacts")
    if unnamed_inbound:
        alarm("warning", "UNNAMED_INBOUND",
              f"{len(unnamed_inbound)} phone-only no-name contact rows since {EPOCH} — touch artifacts, excluded from lead counts")

    # ---- 3b. first outbound CALL per lead entity (mechanism: TYPE_CALL outbound) ----
    for l in leads:
        calls = []
        for cid in l["contact_rows"]:
            convs = _safe(lambda: _get(f"{GHL}/conversations/search?locationId={loc}&contactId={cid}", ghl_h), {})
            for cv in convs.get("conversations", []) or []:
                msgs = _safe(lambda: _get(f"{GHL}/conversations/{cv['id']}/messages", ghl_h), {})
                for m in (msgs.get("messages", {}) or {}).get("messages", []) or []:
                    if m.get("messageType") == "TYPE_CALL" and m.get("direction") == "outbound":
                        calls.append(m.get("dateAdded"))
        if calls and l["created"]:
            t0 = datetime.fromisoformat(l["created"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(min(calls).replace("Z", "+00:00"))
            l["first_call_min"] = round((t1 - t0).total_seconds() / 60, 1)

    # ---- 4. HCP estimates (mechanism: calendar + work_status lifecycle) ----
    cust_cache = {}
    def cust(cid):
        if cid not in cust_cache:
            cust_cache[cid] = _safe(lambda: _get(f"{HCP}/customers/{cid}", hcp_h), {})
        return cust_cache[cid]
    estimates = _safe(lambda: _get(f"{HCP}/estimates?page_size=100&sort_by=created_at&sort_direction=desc", hcp_h), {}).get("estimates", [])
    by_phone = {l["phone10"]: l for l in leads if l["phone10"]}
    est_rows, est_phones = [], set()
    for e in estimates:
        if (e.get("created_at") or "")[:10] < EPOCH: continue
        cid = (e.get("customer") or {}).get("id")
        c = e.get("customer") or {}
        ph = _phone10(c.get("mobile_number") or cust(cid).get("mobile_number"))
        est_phones.add(ph)
        ws = (e.get("work_status") or "").lower()
        sched = (e.get("schedule") or {}).get("scheduled_start")
        converted = "job" in ws  # 'created job from estimate' — VERIFIED conversion node
        est_rows.append({"number": e.get("estimate_number"), "customer": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
                         "created": e.get("created_at"), "scheduled_start": sched, "work_status": e.get("work_status"),
                         "converted": converted, "lead_source": c.get("lead_source"),
                         "ad_lead": ph in by_phone,
                         "no_show_risk": bool(sched and sched < now.strftime("%Y-%m-%dT%H:%M:%SZ") and ws == "scheduled")})
        l = by_phone.get(ph)
        if l:
            l["booked"] = True
            l["est_status"] = e.get("work_status")

    # ---- 5. HCP jobs = WON + revenue (mechanism: job created; amounts VERIFIED cents) ----
    jobs = _safe(lambda: _get(f"{HCP}/jobs?page_size=100&sort_by=created_at&sort_direction=desc", hcp_h), {}).get("jobs", [])
    job_rows = []
    for j in jobs:
        if (j.get("created_at") or "")[:10] < EPOCH: continue
        ws = (j.get("work_status") or "").lower()
        if "canceled" in ws or "cancelled" in ws: continue
        c = j.get("customer") or {}
        ph = _phone10(c.get("mobile_number") or cust(c.get("id")).get("mobile_number"))
        amt = _dollars(j.get("total_amount"))
        row = {"invoice": j.get("invoice_number"), "customer": f"{c.get('first_name','')} {c.get('last_name','')}".strip(),
               "created": j.get("created_at"), "work_status": j.get("work_status"),
               "amount": amt, "outstanding": _dollars(j.get("outstanding_balance")),
               "lead_source": c.get("lead_source"), "ad_lead": ph in by_phone}
        job_rows.append(row)
        l = by_phone.get(ph)
        if l:
            l["won"] = True
            l["revenue"] += amt

    # ---- 5b. AD INTELLIGENCE: trailing-7d per-ad + Maly Golden Threshold verdicts ----
    week_ago = (now.astimezone(ET) - timedelta(days=7)).strftime("%Y-%m-%d")
    ad7 = _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
        "level": "ad", "time_range": json.dumps({"since": week_ago, "until": today}),
        "fields": "ad_id,ad_name,spend,impressions,ctr", "limit": 100, "access_token": fb_tok})), {"data": []})
    ledger_by_ad = {}
    for l in ledger:
        ledger_by_ad[l["ad"]] = ledger_by_ad.get(l["ad"], 0) + 1
    ad_intel = []
    for r in ad7.get("data", []):
        sp = float(r.get("spend", 0)); ctr = float(r.get("ctr", 0))
        nm = r.get("ad_name", "?"); n = ledger_by_ad.get(nm, 0)
        cpl = sp / n if n else None
        if sp < 30: verdict = "LEARNING"
        elif cpl is not None and cpl <= 30 and ctr >= 1.5: verdict = "SCALE-worthy"
        elif (cpl is not None and cpl > 50 and sp >= 75) or (n == 0 and sp >= 60): verdict = "CUT-candidate"
        else: verdict = "WATCH"
        ad_intel.append({"ad": nm, "spend7d": round(sp, 2), "ctr7d": round(ctr, 2),
                         "leads7d": n, "cpl7d": round(cpl, 2) if cpl else None, "verdict": verdict})
    ad_intel.sort(key=lambda a: -a["spend7d"])

    # ---- 6. Watchdog ----
    ghl_if_by_day = {}
    for l in leads:
        if l["source"] == "instant_form":
            d = l["created"][:10]; ghl_if_by_day[d] = ghl_if_by_day.get(d, 0) + 1
    for d, n in ledger_by_day.items():
        if d < today and ghl_if_by_day.get(d, 0) < n:
            alarm("critical", "MAPPING_BROKEN", f"{d}: {n} form submissions but only {ghl_if_by_day.get(d,0)} reached GHL")
    last3 = sorted([d for d in daily if d < today])[-3:]
    sp3 = sum(daily[d].get("A", {}).get("spend", 0) for d in last3)
    ld3 = sum(ledger_by_day.get(d, 0) for d in last3)
    if sp3 > 100 and (ld3 == 0 or sp3 / max(ld3, 1) > 50):
        alarm("serious", "CPL_ALARM", f"Campaign A trailing-3d CPL ${sp3/max(ld3,1):.0f} (${sp3:.0f}/{ld3} leads)")
    for d in last3:
        if daily.get(d, {}).get("A", {}).get("spend", 0) > 50 and ledger_by_day.get(d, 0) == 0:
            alarm("serious", "ZERO_LEAD", f"{d}: ${daily[d]['A']['spend']:.0f} spent, zero form submissions")
    yday = (now.astimezone(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
    ad_ins = _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
        "level": "ad", "time_range": json.dumps({"since": yday, "until": yday}),
        "fields": "ad_id,spend", "limit": 100, "access_token": fb_tok})), {"data": []})
    spent_ids = {r.get("ad_id") for r in ad_ins.get("data", []) if float(r.get("spend", 0)) > 0}
    for ad_id, expected_cr in AD_ROSTER.items():
        st = _safe(lambda: _get(f"{FB}/{ad_id}?fields=effective_status,creative{{id}}&access_token={fb_tok}"), {})
        spent_today = {r.get("ad_id") for r in _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
            "level": "ad", "time_range": json.dumps({"since": today, "until": today}),
            "fields": "ad_id,spend", "limit": 100, "access_token": fb_tok})), {"data": []}).get("data", []) if float(r.get("spend", 0)) > 0}
        if st.get("effective_status") == "ACTIVE" and ad_id not in spent_ids and ad_id not in spent_today and daily.get(yday):
            alarm("warning", "AD_STALLED", f"ad {ad_id} ACTIVE, $0 on {yday} AND $0 so far today — genuinely stalled")
        cr = (st.get("creative") or {}).get("id")
        if cr and cr != expected_cr:
            alarm("warning", "SCHEMA_DRIFT", f"ad {ad_id} creative changed to {cr} (expected {expected_cr})")
    hcust = _safe(lambda: _get(f"{HCP}/customers?page_size=50&sort_by=created_at&sort_direction=desc", hcp_h), {}).get("customers", [])
    for c in hcust:
        if (c.get("created_at") or "") <= HCP_KILL_UTC: continue
        ph = _phone10(c.get("mobile_number"))
        if ph and ph in by_phone and ph not in est_phones:
            alarm("critical", "HCP_PUSH_RESURRECTED", f"lead {by_phone[ph]['name']} appeared in HCP with no estimate")
    sla_breaches, sla_samples = 0, []
    for l in leads:
        if l["source"] not in ("instant_form", "funnel_page"):
            continue
        t0 = datetime.fromisoformat(l["created"].replace("Z", "+00:00")).astimezone(ET)
        in_biz = BIZ_START <= t0.hour < BIZ_END
        if l["first_call_min"] is not None:
            if in_biz:
                sla_samples.append(l["first_call_min"])
                if l["first_call_min"] > 15: sla_breaches += 1
        elif in_biz and (now - t0.astimezone(timezone.utc)).total_seconds() > 7200:
            sla_breaches += 1
            alarm("serious", "SLA_BREACH", f"{l['name']} ({l['source']}) uncalled for >2h")
    hr_now = now.astimezone(ET).hour
    if BIZ_START + 3 <= hr_now < BIZ_END:
        hourly = _safe(lambda: _get(f"{FB}/act_506325703451497/insights?" + urllib.parse.urlencode({
            "level": "account", "breakdowns": "hourly_stats_aggregated_by_advertiser_time_zone",
            "fields": "impressions", "time_range": json.dumps({"since": today, "until": today}),
            "access_token": fb_tok})), {"data": []})
        by_hr = {}
        for row in hourly.get("data", []):
            try: by_hr[int(row["hourly_stats_aggregated_by_advertiser_time_zone"][:2])] = int(row.get("impressions", 0))
            except Exception: pass
        recent = sum(by_hr.get(h, 0) for h in (hr_now - 1, hr_now - 2))
        earlier = [by_hr.get(h, 0) for h in range(BIZ_START, hr_now - 2)]
        if earlier and (sum(earlier) / len(earlier)) > 100 and recent < 40:
            alarm("serious", "INTRADAY_STALL", f"last 2 hours ~{recent} impressions vs earlier avg {int(sum(earlier)/len(earlier))}/hr — delivery stopped mid-day")
    try:
        page_html = urllib.request.urlopen("https://go.centralfloridatrimlight.com/", timeout=20).read().decode()
        if "What are you interested in having done?" not in page_html:
            alarm("critical", "INFRA_DOWN", "funnel page serving but canonical form marker missing")
    except Exception as e:
        alarm("critical", "INFRA_DOWN", f"funnel page unreachable: {str(e)[:80]}")
    # ---- SIGNAL INTEGRITY (deterministic node lock): verify each node's shape; alarm, never guess ----
    integrity = []
    if not ins.get("data"): integrity.append("meta-insights returned no rows")
    if jobs and not all(isinstance(j.get("total_amount"), int) for j in jobs[:5]):
        integrity.append("hcp job total_amount no longer integer cents")
    if any(_dollars(j.get("total_amount")) > 100000 for j in jobs):
        integrity.append("hcp job amount > $100k — unit assumption (cents) may have changed")
    if estimates and not all("work_status" in e for e in estimates[:5]):
        integrity.append("hcp estimate work_status field missing")
    known_ws = {"scheduled", "created job from estimate", "unscheduled", "pro canceled", "user canceled",
                "complete", "in progress", "needs scheduling", "complete rated", "complete unrated", "on my way", "started"}
    novel = {(e.get("work_status") or "").lower() for e in estimates if (e.get("work_status") or "").lower() not in known_ws}
    if novel: integrity.append(f"novel estimate work_status values (extend contract): {sorted(novel)[:4]}")
    if rows and not all(c.get("dateAdded") for c in rows[:5]):
        integrity.append("ghl contact dateAdded missing")
    if len(contacts) >= 1000: integrity.append("ghl pagination hit cap — cohort may be truncated")
    for msg in integrity:
        alarm("critical", "SIGNAL_INTEGRITY", msg)
    if not alarms:
        alarm("good", "ALL_CLEAR", "every watchdog check passed")

    # ---- 7. Assemble ----
    def funnel_row(sel):
        return {"leads": len(sel),
                "contacted": sum(1 for l in sel if l.get("first_call_min") is not None),
                "booked": sum(1 for l in sel if l.get("booked")),
                "won": sum(1 for l in sel if l.get("won")),
                "revenue": round(sum(l.get("revenue", 0) for l in sel), 2)}
    spend_a = sum(v.get("A", {}).get("spend", 0) for v in daily.values())
    spend_b = sum(v.get("B", {}).get("spend", 0) for v in daily.values())
    funnel = {"instant_form": funnel_row([l for l in leads if l["source"] == "instant_form"]) | {"spend": round(spend_a, 2)},
              "funnel_page": funnel_row([l for l in leads if l["source"] == "funnel_page"]) | {"spend": round(spend_b, 2)},
              "other": funnel_row([l for l in leads if l["source"] == "other"]) | {"spend": 0},
              "total": funnel_row(leads) | {"spend": round(spend_a + spend_b, 2)}}
    sla_samples.sort()
    return {
        "generated_at": now.isoformat(), "epoch": EPOCH, "contract_version": SIGNAL_CONTRACT_VERSION,
        "daily": [{"date": d,
                   "A": {k: round(v, 2) if isinstance(v, float) else v for k, v in daily[d].get("A", {}).items()},
                   "B": {k: round(v, 2) if isinstance(v, float) else v for k, v in daily[d].get("B", {}).items()},
                   "ledger_leads": ledger_by_day.get(d, 0)} for d in sorted(daily)],
        "funnel": funnel,
        "estimates": sorted(est_rows, key=lambda r: r["created"] or "", reverse=True),
        "jobs": sorted(job_rows, key=lambda r: r["created"] or "", reverse=True),
        "ad_intel": ad_intel,
        "jobs_summary": {"count": len(job_rows), "revenue": round(sum(r["amount"] for r in job_rows), 2),
                         "converted_estimates": sum(1 for r in est_rows if r["converted"])},
        "sla": {"median_min": sla_samples[len(sla_samples)//2] if sla_samples else None,
                "breaches": sla_breaches, "calls_measured": len(sla_samples)},
        "leads": sorted([{k: v for k, v in l.items() if k != "contact_rows"} for l in leads],
                        key=lambda l: l["created"], reverse=True),
        "unnamed_inbound": unnamed_inbound,
        "alarms": alarms,
    }
