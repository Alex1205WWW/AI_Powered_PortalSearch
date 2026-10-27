"""End-to-end smoke test against a running panel (Day 1 gate).

Usage: python scripts/smoke.py [base_url]        default http://localhost:8000
Env:   ADMIN_USERNAME (default andreia), SMOKE_PASSWORD (default dev-password)

Checks: /healthz, login redirect, login, create buyer, create criteria through the real form,
read the detail page back, invalid criteria are rejected with messages, buyers page timing.
"""
from __future__ import annotations

import os
import sys
import time

import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
user = os.environ.get("ADMIN_USERNAME", "andreia")
password = os.environ.get("SMOKE_PASSWORD", "dev-password")

c = httpx.Client(base_url=base, follow_redirects=False, timeout=30, verify=False)

r = c.get("/healthz")
assert r.status_code == 200 and r.json()["ok"], r

r = c.get("/buyers")
assert r.status_code == 303 and "/login" in r.headers["location"], "unauthenticated access must redirect"

r = c.post("/login", data={"username": user, "password": "wrong"})
assert r.status_code == 200 and "incorretos" in r.text, "wrong password must be rejected"

r = c.post("/login", data={"username": user, "password": password, "next": "/buyers"})
assert r.status_code == 303 and r.headers["location"] == "/buyers", r.text[:300]

label = f"Smoke {int(time.time())}"
r = c.post("/buyers", data={"label": label})
assert r.status_code == 303, r.text[:300]
buyer_url = r.headers["location"].split("?")[0]
buyer_id = int(buyer_url.rsplit("/", 1)[1])

form = {
    "buyer_id": str(buyer_id), "transaction": "sale", "estate_types": ["moradia"],
    "typology_min": "2", "typology_max": "", "price_min": "400.000", "price_max": "650000",
    "must_have": ["exterior"], "nice_to_have": ["garagem"],
    "zones": ["lisboa/lisboa", "lisboa/oeiras", "lisboa/sintra", "lisboa/loures"],
    "include_private_owner": "on", "unknown_policy": "show",
    "source_text": "moradia t2 ou mais, com terreno, Lisboa Oeiras Sintra Loures, 400 a 650 mil",
}
r = c.post("/criteria/new", data=form)
assert r.status_code == 303, r.text[:800]

r = c.get(buyer_url)
assert r.status_code == 200
html = r.text
for needle in [label, "T2 ou mais", "400.000 € a 650.000 €", "Oeiras", "Sintra", "Terreno / espaço exterior", "Garagem"]:
    assert needle in html, f"missing on detail page: {needle}"

r = c.post("/criteria/new", data={"buyer_id": str(buyer_id), "estate_types": ["apartamento"], "price_min": "500000", "price_max": "400000"})
assert r.status_code == 200 and "Preço mínimo maior" in r.text and "zona" in r.text, "validation errors must be shown"

r = c.post("/buyers", data={"label": label})
assert r.status_code == 200 and "Já existe" in r.text, "duplicate label must be rejected"

t0 = time.perf_counter()
r = c.get("/buyers")
elapsed_ms = (time.perf_counter() - t0) * 1000
assert r.status_code == 200
buyers_on_page = r.text.count('<a href="/buyers/')

r = c.get("/sources")
assert r.status_code == 200 and "Imovirtual" in r.text

print(f"smoke ok: buyer {buyer_id} '{label}' created with criteria; buyers page {elapsed_ms:.0f} ms for {buyers_on_page} buyers")
