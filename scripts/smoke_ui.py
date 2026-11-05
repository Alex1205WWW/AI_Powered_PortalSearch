"""Walk every page of the panel and press every button, then put everything back.

`smoke.py` covers the Day 1 gate (login, create a buyer, create criteria). This one is the full UI pass:
every page renders, every form posts, every control does what it claims, and the layout primitives the
2026-11-05 redesign introduced are actually present rather than merely written.

Safe to run against production. Everything it changes it restores, and the throwaway buyer it creates is
deleted at the end. It deliberately never calls the split endpoint: splitting a property is a real dedup
decision with no undo.

Usage: python -m scripts.smoke_ui [base_url]
Env:   ADMIN_USERNAME (default andreia), SMOKE_PASSWORD
"""
from __future__ import annotations

import os
import re
import sys
import time

import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
user = os.environ.get("ADMIN_USERNAME", "andreia")
password = os.environ.get("SMOKE_PASSWORD", "dev-password")

ok_count = fail_count = 0
notes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    global ok_count, fail_count
    if condition:
        ok_count += 1
        print(f"  ok    {name}" + (f"  [{detail}]" if detail else ""))
    else:
        fail_count += 1
        print(f"  FAIL  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(condition)


c = httpx.Client(base_url=base, follow_redirects=False, timeout=60, verify=False)


def page(path: str) -> str:
    r = c.get(path)
    check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")
    return r.text if r.status_code == 200 else ""


print("\n1. the shell: assets, theme toggle, navigation")
r = c.post("/login", data={"username": user, "password": password, "next": "/"})
if r.status_code != 303:
    sys.exit(f"login failed: HTTP {r.status_code}. Set SMOKE_PASSWORD.")
if "session" not in c.cookies:
    # In production the session cookie is `Secure`, so it is simply dropped over plain http:// and every
    # page then redirects to /login - which looks like a hundred unrelated UI failures. Say so instead.
    sys.exit(f"logged in but no session cookie was kept.\n"
             f"  {base} is http:// and the production cookie is Secure.\n"
             f"  Run it against the https:// URL instead.")
home = c.get("/").text
if "/login" in home[:400] and "theme-toggle" not in home:
    sys.exit("the session is not being carried; aborting rather than reporting false failures.")

# A deploy that the browser never fetches is invisible; this is what makes the URL move with the file.
versioned = re.findall(r"/static/([a-z.]+)\?v=(\d+)", home)
check("css and js are cache-busted", {n for n, _ in versioned} >= {"app.css", "pico.min.css"},
      ", ".join(sorted(n for n, _ in versioned)))
check("theme toggle is present", 'id="theme-toggle"' in home)
check("both theme icons ship", home.count('class="i-light"') == 1 and home.count('class="i-dark"') == 1)
check("icons have intrinsic size", home.count('width="18" height="18"') == 2,
      "an SVG with only a viewBox collapses without CSS")
check("theme is applied before paint", "imoveis-theme" in home.split("</head>")[0],
      "otherwise every load flashes the wrong theme")
for label in ("Compradores", "Imóveis", "Nova pesquisa", "Mediadoras", "Fontes", "Faltaram", "Configurações"):
    check(f"nav has {label}", f">{label}</a>" in home)

print("\n2. dashboard")
check("stat tiles render", home.count('class="stat') >= 4, f"{home.count('class=')} classes")
check("tiles are links to the lists", 'class="stat card-link" href="/properties?state=novo"' in home)
check("per-buyer panels render", 'class="panel-grid"' in home and 'class="panel"' in home)
check("sources table is framed", 'class="table-wrap"' in home)
# The bug of 2026-11-05: `.card-link{display:block}` came after `.stat{display:flex}` and flattened the
# tile into one inline sentence. The CSS is the only place that can be checked from here.
css = c.get("/static/app.css").text
block_in_card_link = re.search(r"\.card-link\s*\{[^}]*display\s*:\s*block", css)
check("card-link does not override the tile layout", block_in_card_link is None,
      "display:block here flattens .stat")
check("stat tiles carry their own surface", re.search(r"\.stat\s*\{[^}]*background", css) is not None,
      "a tile is an <a>/<div>, so it never matches the `article` rule")

print("\n3. buyers: create, pause, resume")
label = f"Smoke UI {int(time.time())}"
r = c.post("/buyers", data={"label": label})
created = check("create buyer", r.status_code == 303, f"HTTP {r.status_code}")
buyer_url = r.headers["location"].split("?")[0] if created else ""
buyer_id = int(buyer_url.rsplit("/", 1)[1]) if created else 0
listing = page("/buyers")
check("new buyer appears in the list", label in listing)
detail = page(buyer_url) if created else ""
check("detail page shows the buyer", label in detail)

if created:
    r = c.post(f"/buyers/{buyer_id}/pause")
    check("pause button", r.status_code == 303, f"HTTP {r.status_code}")
    check("paused state is shown", "Retomar" in c.get(buyer_url).text)
    r = c.post(f"/buyers/{buyer_id}/resume")
    check("resume button", r.status_code == 303, f"HTTP {r.status_code}")
    check("active state is shown", "Pausar" in c.get(buyer_url).text)

print("\n4. criteria: the form, validation, creation")
form_html = page("/criteria/new")
for field in ("buyer_id", "transaction", "estate_types", "typology_min", "price_min",
              "zones", "must_have", "nice_to_have", "exclude", "unknown_policy", "source_text"):
    check(f"form field {field}", f'name="{field}"' in form_html)
check("zones offer whole districts", 'value="lisboa"' in form_html)
check("the obligatory/desirable switch is there", 'name="unknown_policy"' in form_html and "hide" in form_html)

r = c.post("/criteria/new", data={"buyer_id": str(buyer_id), "estate_types": ["apartamento"],
                                  "price_min": "500000", "price_max": "400000"})
check("inverted prices are refused with a message", r.status_code == 200 and "Preço mínimo maior" in r.text)

criteria_form = {
    "buyer_id": str(buyer_id), "transaction": "sale", "estate_types": ["apartamento"],
    "typology_min": "1", "typology_max": "2", "price_min": "100000", "price_max": "120000",
    "must_have": ["elevador"], "zones": ["lisboa/lisboa"], "unknown_policy": "show",
    "source_text": "smoke ui",
}
r = c.post("/criteria/new", data=criteria_form)
check("create criteria", r.status_code == 303, f"HTTP {r.status_code}")
check("criteria shows on the buyer", "T1 a T2" in c.get(buyer_url).text or "elevador" in c.get(buyer_url).text.lower())

print("\n5. properties: filters, export, state buttons")
props = page("/properties")
check("results table renders", 'class="table-properties"' in props)
check("export button", "/properties/export.xlsx" in props)
for control in ("buyer", "grade", "state", "zone", "estate", "price_min", "price_max", "rooms_min", "q"):
    check(f"filter {control}", f'name="{control}"' in props)
for query in ("?state=novo", "?grade=confirmada", "?grade=possivel", "?price_min=300000&price_max=450000",
              "?rooms_min=1&rooms_max=2", "?q=lisboa"):
    r = c.get(f"/properties{query}")
    check(f"filter {query}", r.status_code == 200, f"HTTP {r.status_code}")
r = c.get("/properties?price_min=99000000")
check("an empty result explains itself", r.status_code == 200 and "Nenhum" in r.text)

r = c.get("/properties/export.xlsx")
check("Excel export downloads", r.status_code == 200 and len(r.content) > 5000,
      f"{len(r.content)} bytes" if r.status_code == 200 else f"HTTP {r.status_code}")
check("export is a real xlsx", r.content[:2] == b"PK" if r.status_code == 200 else False)

# Press a real state button on one of her matches, then put it back exactly as it was.
match_ids = re.findall(r'action="/matches/(\d+)/state"', props)
if match_ids:
    mid = match_ids[0]
    current = re.search(rf'action="/matches/{mid}/state".*?<option value="([a-z]+)" selected', props, re.S)
    before = current.group(1) if current else "novo"
    target = "analisado" if before != "analisado" else "novo"
    r = c.post(f"/matches/{mid}/state", data={"state": target, "back": "/properties"})
    check("state button changes the state", r.status_code == 303, f"{before} -> {target}")
    after = c.get("/properties").text
    check("the new state is shown", re.search(rf'action="/matches/{mid}/state".*?<option value="{target}" selected', after, re.S) is not None)
    r = c.post(f"/matches/{mid}/state", data={"state": before, "back": "/properties"})
    check("state restored", r.status_code == 303, f"back to {before}")
else:
    notes.append("no matches on the first page, so the state button was not exercised")

print("\n6. the other pages")
agencies = page("/agencies")
check("agencies page lists sites", "Mediadoras" in agencies)
check("agency add form", 'action="/agencies/add"' in agencies or 'name="url"' in agencies)
sources = page("/sources")
check("sources page", "Imovirtual" in sources)
check("reload promise is stated", "entra em vigor" in sources, "she must know a toggle is not instant")
findings = page("/findings")
check("findings page", "Faltaram" in findings or "faltou" in findings.lower())
settings = page("/settings")
check("settings page", "Configurações" in settings or "alert" in settings.lower())

print("\n7. source toggle, and put it back")
toggle = re.search(r'action="/sources/(\d+)/toggle"><input type="hidden" name="enabled" value="([01])"', sources)
if toggle:
    sid, want = toggle.group(1), toggle.group(2)
    r = c.post(f"/sources/{sid}/toggle", data={"enabled": want})
    check("source toggle", r.status_code == 303, f"source {sid} -> {want}")
    r = c.post(f"/sources/{sid}/toggle", data={"enabled": "0" if want == "1" else "1"})
    check("source restored", r.status_code == 303)
else:
    notes.append("no source toggle found to exercise")

print("\n8. errors behave")
r = c.get("/buyers/99999999")
check("unknown buyer is a clean 404, not a 500", r.status_code in (404, 303), f"HTTP {r.status_code}")
r = c.get("/properties?price_min=abc")
check("rubbish in a numeric filter does not 500", r.status_code in (200, 303, 422), f"HTTP {r.status_code}")

print("\n9. cleanup")
if created:
    # Deleting asks for the buyer's own name to be typed, because "Apagar" sits next to "Pausar" and a
    # mis-click would destroy a real client's history. Prove the guard holds before using the real path.
    r = c.post(f"/buyers/{buyer_id}/delete", data={"confirm": "qualquer coisa"})
    check("delete without the typed name is refused", r.status_code == 303 and label in c.get("/buyers").text,
          "the guard that stops a mis-click")

    r = c.post(f"/buyers/{buyer_id}/delete", data={"confirm": label})
    check("delete with the typed name", r.status_code == 303, f"HTTP {r.status_code}")
    check("it is gone from the list", label not in c.get("/buyers").text)

r = c.post("/logout")
check("logout", r.status_code == 303)
check("session really ended", c.get("/buyers").status_code == 303)

for note in notes:
    print(f"  note  {note}")
print(f"\n{ok_count} ok, {fail_count} failed")
sys.exit(1 if fail_count else 0)
