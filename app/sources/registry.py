"""Seed definitions of the known sources (Milestone1_Plan.md §1) and the adapter registry.
Adapters register themselves here on later days: ADAPTERS["imovirtual"] = ImovirtualAdapter."""
from __future__ import annotations

SOURCE_SEED: list[dict] = [
    {"key": "imovirtual", "kind": "portal", "name": "Imovirtual", "base_url": "https://www.imovirtual.com",
     "enabled": True, "schedule_minutes": 180, "crawl_delay_s": 2.0,
     "notes": "Fase 1 primary. __NEXT_DATA__ JSON. Content-Signal ai-input=no: listing content never goes to an LLM."},
    {"key": "remax", "kind": "portal", "name": "RE/MAX", "base_url": "https://remax.pt",
     "enabled": True, "schedule_minutes": 360, "crawl_delay_s": 1.5,
     "notes": "Fase 1 second portal. Sitemap + /pt/imoveis/<slug>/<id> listingEncoded. robots: Disallow /api (never used). www.remax.pt 301s to remax.pt."},
    {"key": "idealista_email", "kind": "email", "name": "Idealista (alertas por email)", "base_url": "imap://",
     "enabled": True, "schedule_minutes": 3, "crawl_delay_s": 0.0,
     "notes": "Saved-search alert emails read from the dedicated mailbox over IMAP. No direct access to idealista.pt."},
    {"key": "century21", "kind": "portal", "name": "Century21", "base_url": "https://www.century21.pt",
     "enabled": False, "schedule_minutes": 720, "crawl_delay_s": 2.0,
     "notes": "Viable (sitemap-ref-0.xml -> /ref/<REF>) but only as a paid +1 day. Disabled."},
    {"key": "era", "kind": "portal", "name": "ERA", "base_url": "https://www.era.pt",
     "enabled": False, "schedule_minutes": None, "crawl_delay_s": 2.0,
     "notes": "Not readable: listings rendered client-side (DotNetNuke + React). Fase 2 (browser engine)."},
    {"key": "supercasa", "kind": "portal", "name": "SUPERCASA", "base_url": "https://supercasa.pt",
     "enabled": False, "schedule_minutes": None, "crawl_delay_s": 2.0,
     "notes": "Cloudflare challenge on every page (HTTP 403). Fase 2 (browser engine)."},
    {"key": "olx", "kind": "portal", "name": "OLX", "base_url": "https://www.olx.pt",
     "enabled": False, "schedule_minutes": None, "crawl_delay_s": 2.0,
     "notes": "HTTP 403 even on robots.txt. Fase 2 / out."},
    {"key": "casayes", "kind": "portal", "name": "CasaYes", "base_url": "https://casa.sapo.pt",
     "enabled": False, "schedule_minutes": None, "crawl_delay_s": 2.0,
     "notes": "Client-rendered shell. Fase 2 (browser engine)."},
    # Not a place listings come FROM: the channel they go TO. It is a source row because `services.health`
    # already owns exactly the state machine delivery needs - ok / degraded / blocked, a failure counter,
    # last_ok_at, last_error - and `banner()` already puts that on every page. Reusing it means a dead
    # Telegram channel is visible with no new table and no new UI concept. Deliberately NOT in
    # health.ANNOUNCED_KINDS: announcing a Telegram outage over Telegram is the one message that cannot
    # arrive. `schedule_minutes` is None because nothing crawls it.
    {"key": "telegram", "kind": "channel", "name": "Notificações (Telegram)",
     "base_url": "https://api.telegram.org", "enabled": True, "schedule_minutes": None, "crawl_delay_s": 0.0,
     "notes": "Canal de saída dos alertas. O estado é atualizado a cada envio em services/notifications.py."},
]

ADAPTERS: dict[str, type] = {}


def register(key: str):
    def decorator(cls):
        ADAPTERS[key] = cls
        return cls
    return decorator


def adapter_for(source):
    """The adapter class for a source row, or None.

    Agency sources are created at runtime with keys like `agency:dils.pt`, so they can never be in
    `ADAPTERS`, which is keyed by the fixed portal keys. Resolving by `kind` keeps every caller from having
    to know that."""
    if getattr(source, "kind", None) == "agency":
        from app.sources.agency_generic import AgencyAdapter

        return AgencyAdapter if (source.profile or {}).get("index", {}).get("listing_url_re") else None
    return ADAPTERS.get(source.key)
