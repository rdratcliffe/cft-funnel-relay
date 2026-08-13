# Post-Freeze Migration Notes (relay -> DealerOS)

This app currently owns four production functions. When the DealerOS beta freeze lifts, they can
move into DealerOS — or stay here indefinitely ($5/mo, zero DealerOS coupling). Nothing about the
session that built this is required for it to keep running.

| Function | Where it lives here | DealerOS migration path |
|---|---|---|
| Funnel lead endpoint (/funnel/lead) | app.py `process()` | PR #267 (already written) — swap FORM_ENDPOINT constant in site/index.html, done |
| Funnel Scorecard (/dash + collector.py) | stdlib-only, env-configured | Mount as a DealerOS route/job — BUT keep pulling from the systems of record per the Signal Contract (Meta ledger/GHL/HCP), never from DealerOS's mirror tables |
| Dupe janitor (janitor.py) | 30-min loop | Retire ONLY after DealerOS voice call-logging does find-or-create upsert by E.164 phone (the root-cause bug filed in vault Signal Contract) |
| Lead nurture drip (_drip_pass) | env-gated, copy in DRIP_MSG_1..5 | Move to DealerOS scheduler or keep; GHL tags are the state, so it's restart/migrate-safe |

Config = env vars only: GHL_KEY, GHL_LOCATION, PIPELINE_ID, STAGE_ID, FACEBOOK_ACCESS_TOKEN,
HCP_API_KEY, DASH_KEY, SENDGRID_API_KEY, ALERT_EMAIL, DRIP_ENABLED, DRIP_START, DRIP_MSG_1..5.
Deploys do NOT auto-trigger on push: POST /v2/apps/{id}/deployments (force_build) after pushing.
Signal definitions are governed by the vault: business-brain/09-Systems/Signal Contract.md —
code changes to collector.py require a matching contract edit, same pass.
