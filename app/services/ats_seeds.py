"""
Seed lists of well-known tech companies per ATS.

Every slug below was verified against the live ATS API on 2026-07-02 — a wrong
slug contributes nothing (the board API 404s), so only verified entries ship.
Merged into the fetch when ATS_SEED_COMPANIES is enabled; excluded_companies
from the profile still filters matches downstream.

Workday entries are "tenant:host:site" triples (see sources/workday.py).
"""

SEED_ATS_SLUGS: dict[str, list[str]] = {
    "greenhouse": [
        "stripe", "airbnb", "robinhood", "coinbase", "databricks", "gitlab",
        "cloudflare", "doordashusa", "instacart", "pinterest", "reddit", "lyft",
        "twitch", "mongodb", "datadog", "elastic", "asana", "figma", "brex",
        "affirm", "flexport", "samsara", "vercel", "airtable", "discord",
        "duolingo", "gusto", "okta", "pagerduty", "scaleai", "sofi",
        "squarespace", "zscaler", "chime", "checkr",
    ],
    "lever": [
        "netflix", "palantir", "plaid", "voleon", "mistral", "zoox",
        "matchgroup", "spotify", "kraken", "octoenergy", "highspot",
        "outreach", "veeva",
    ],
    "ashby": [
        "openai", "linear", "notion", "ramp", "deel", "replit", "supabase",
        "posthog", "cursor", "perplexity", "vanta", "mercury", "clever", "zip",
        "hightouch", "sierra", "docker", "modal", "elevenlabs",
    ],
    "workday": [
        "nvidia:wd5:NVIDIAExternalCareerSite",
        "salesforce:wd12:External_Career_Site",
        "adobe:wd5:external_experienced",
        "workday:wd5:Workday",
    ],
}
