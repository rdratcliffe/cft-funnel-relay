"""GHL duplicate-contact janitor — compensating control for the Lc-Phone per-call-leg
duplication defect (dedupe preference is OFF yet legs still spawn contacts; see vault
Signal Contract, Lawrence Pleis audit-log evidence 2026-08-12).

SAFETY BY CONSTRUCTION:
- Scope: contacts created since EPOCH only (never touches historical data).
- A duplicate group = >1 contact rows sharing the same 10-digit phone.
- Canonical row = earliest-created row that has a name. No named row -> group is skipped.
- A shell is DELETED only if it is provably data-free: no name AND zero conversations
  AND zero notes AND zero tasks AND zero opportunities, AND older than GRACE_MIN minutes
  (gives GHL's name-lookup enrichment time to fill names in).
- Anything with data (or a name) is never deleted: it gets tag 'duplicate-merge-needed'
  for Michelle's one-click merge, and stays on the dashboard alarm list.
Every action is recorded and surfaced on the dashboard (JANITOR entries)."""
import os, json, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

EPOCH = "2026-08-10"
GRACE_MIN = 60
GHL = "https://services.leadconnectorhq.com"
UA = "cft-funnel-relay/1.0"
MERGE_TAG = "duplicate-merge-needed"

def _req(url, method="GET", body=None):
    h = {"Authorization": "Bearer " + os.environ["GHL_KEY"], "Version": "2021-07-28",
         "User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}

def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default

def _phone10(p):
    d = "".join(c for c in str(p or "") if c.isdigit())
    return d[-10:] if len(d) >= 10 else d

def _count(url, key):
    d = _safe(lambda: _req(url), None)
    if d is None:
        return None  # None = COULD NOT VERIFY -> never treat as zero
    v = d.get(key)
    if isinstance(v, list):
        return len(v)
    if isinstance(v, dict):
        inner = v.get(key) or v.get("messages") or []
        return len(inner) if isinstance(inner, list) else None
    return None

def run():
    loc = os.environ["GHL_LOCATION"]
    now = datetime.now(timezone.utc)
    actions = {"deleted_ghosts": [], "tagged_for_merge": [], "skipped": [], "errors": []}

    contacts, start_after = [], None
    for _ in range(10):
        q = {"locationId": loc, "limit": 100}
        if start_after:
            q["startAfterId"] = start_after
        page = _safe(lambda: _req(f"{GHL}/contacts/?" + urllib.parse.urlencode(q)), {"contacts": []})
        batch = page.get("contacts", [])
        if not batch:
            break
        contacts.extend(batch)
        start_after = batch[-1].get("id")
        if batch[-1].get("dateAdded", "9999")[:10] < EPOCH:
            break
    rows = [c for c in {c["id"]: c for c in contacts}.values()
            if (c.get("dateAdded") or "")[:10] >= EPOCH]

    groups = {}
    for c in rows:
        ph = _phone10(c.get("phone"))
        if ph:
            groups.setdefault(ph, []).append(c)

    for ph, grp in groups.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda c: c.get("dateAdded") or "")
        named = [c for c in grp if c.get("firstName") or c.get("lastName")]
        if not named:
            actions["skipped"].append({"phone": ph, "reason": "no named canonical"})
            continue
        canonical = named[0]
        for c in grp:
            if c["id"] == canonical["id"]:
                continue
            cid = c["id"]
            has_name = bool(c.get("firstName") or c.get("lastName"))
            age_min = 1e9
            try:
                age_min = (now - datetime.fromisoformat(c["dateAdded"].replace("Z", "+00:00"))).total_seconds() / 60
            except Exception:
                pass
            label = f"{(c.get('firstName') or '')+' '+(c.get('lastName') or '')}".strip() or "(unnamed)"
            if not has_name and age_min > GRACE_MIN:
                convs = _count(f"{GHL}/conversations/search?locationId={loc}&contactId={cid}", "conversations")
                notes = _count(f"{GHL}/contacts/{cid}/notes", "notes")
                tasks = _count(f"{GHL}/contacts/{cid}/tasks", "tasks")
                opps = _count(f"{GHL}/opportunities/search?location_id={loc}&contact_id={cid}", "opportunities")
                if convs == 0 and notes == 0 and tasks == 0 and opps == 0:
                    ok = _safe(lambda: _req(f"{GHL}/contacts/{cid}", "DELETE"), None)
                    if ok is not None:
                        actions["deleted_ghosts"].append({"phone": ph, "contact_id": cid})
                    else:
                        actions["errors"].append({"contact_id": cid, "op": "delete"})
                    continue
            # data-bearing or named duplicate -> tag for manual merge (idempotent)
            tags = [t.lower() for t in (c.get("tags") or [])]
            if MERGE_TAG not in tags:
                ok = _safe(lambda: _req(f"{GHL}/contacts/{cid}/tags", "POST", {"tags": [MERGE_TAG]}), None)
                if ok is not None:
                    actions["tagged_for_merge"].append({"phone": ph, "contact_id": cid, "name": label})
                else:
                    actions["errors"].append({"contact_id": cid, "op": "tag"})
            else:
                actions["tagged_for_merge"].append({"phone": ph, "contact_id": cid, "name": label, "already": True})
    return {"ran_at": now.isoformat(), "groups_with_dupes": sum(1 for g in groups.values() if len(g) > 1),
            **{k: v for k, v in actions.items()}}
