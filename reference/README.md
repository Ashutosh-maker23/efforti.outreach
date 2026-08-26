# Salvage: old ICP filtering + Apollo pull

The previous `app/icp.py` and `app/apollo.py` were **removed** (reduced to inert
stubs) on 2026-08-21 because the ICP they encoded was the wrong ICP — it hard-
rejected and down-ranked the exact buyers who actually reply (large/scaled
Indian tech, IT-services, GCC and ops-heavy eng orgs; VP/Director/Head of
Engineering/Delivery/Program/IT). The whole ICP + pull is being rebuilt from
scratch against the real profile.

These two files are the **verbatim** old code, kept for reference so we lose
nothing and can cherry-pick the parts that were genuinely good.

- `icp_salvage.py`  — old `app/icp.py` (520 lines)
- `apollo_salvage.py` — old `app/apollo.py` (1032 lines)

> Reference only — not importable as-is (package-relative imports). Copy the
> functions you want back into the rebuilt modules.

## KEEP-LIST — the parts that were good and should return in the rebuild

These are the features explicitly marked "keep, don't change":

### 1. ICP scoring *mechanism* (keep the shape, rebuild the criteria)
- `icp_salvage.score_lead()` — transparent 0–100 score → `{score, verdict,
  reasons, trigger}`, hard-gates first then additive signals. Keep the SHAPE;
  the industry lists / title tiers / band / funding-age weights inside are the
  wrong ICP and get rewritten.
- `icp_salvage.title_tier()` — tiered title → (score_delta, reason). Keep the
  tiering *pattern*; the tiers themselves are inverted for our real buyer.
- Persisted on the Lead: `icp_score` / `icp_reasons` / `trigger` columns
  (in `app/models.py` — untouched).

### 2. Credit frugality — "0 wasted credits" (keep wholesale)
- `apollo_salvage.prescreen_org()` / `icp_salvage.prescreen_org()` — free
  pre-reveal reject on unambiguous non-fits.
- `apollo_salvage.identity_key()` + the `known_ids` / `ident_index` dedupe in
  `pull_apollo` — skip already-owned leads BEFORE paying a reveal.
- `apollo_salvage._search_org_by_domain()` / `_fetch_org()` /
  `enrich_org_by_domain()` — company firmographics via the FREE search tier,
  never the credit-metered `organizations/enrich` endpoint.
- `apollo_salvage._bulk_enrich()` + the `max_reveals` reveal-budget loop —
  reveal only as many as still needed, in bulk batches of 10.
- The `off_icp` bucketing — a paid reveal is NEVER discarded.

### 3. Remote / distributed-team detection (keep as an available signal)
- `icp_salvage.remote_signal()` / `REMOTE_RE` — detect remote-first shops from
  the revealed org's keywords/description (Apollo has no native remote filter).

### 4. Free brand-facts backfill (used by personalization — keep)
- `apollo_salvage.backfill_company_facts()` / `complete_lead()` /
  `backfill_pool_company_facts()` / `rescore_lead()` — fill missing company
  facts + ICP score from the free lookup (0 credits).

## Wiring contract the rebuild must satisfy

Call sites outside these two files that must keep working:
- `app/main.py` imports: `COMPANY_TRAITS, DEFAULT_KEYWORDS, INDUSTRY_OPTIONS,
  SIZE_PRESETS, industry_hints, industry_tags, trait_hints, trait_tags,
  preview_apollo, pull_apollo` (+ lazy `complete_lead`, `rescore_lead`).
- `app/enrich.py` imports: `backfill_company_facts, org_lookup_blocked_reason`.
- rebuilt `apollo.py` imports from rebuilt `icp.py`: the scoring/gate entry points.
