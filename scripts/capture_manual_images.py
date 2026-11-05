"""Capture the manual's screenshots from the real panel.

    python -m scripts.capture_manual_images https://andreia-imoveis.duckdns.org
    (reads the password from PANEL_PASSWORD, or falls back to SMOKE_PASSWORD)

Deliverable 16 promises a manual in Portuguese with images, and `docs/manual_pt/manual.md` says the
images will be taken from the real panel with her own data - "um manual com imagens de dados inventados
seria pior do que um manual sem imagens". This is how they are taken, so they can be retaken after any UI
change instead of being a one-off nobody can reproduce.

Uses the Chrome already installed on the machine over its DevTools protocol: no browser automation
dependency to install, and `websockets` is already a dependency of this project. Chrome runs headless in a
throwaway profile, logs in through the real form, and screenshots each page.

Read-only: it visits pages and takes pictures. It never posts a form other than the login, so it is safe
against production - unlike `scripts/smoke_ui.py`, which deliberately changes things and puts them back.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from websockets.sync.client import connect

OUT_DIR = pathlib.Path("docs/manual_pt/images")
WIDTH, HEIGHT = 1440, 980

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

# (file name, path, full page?) - `full` only for pages that are short enough to stay readable. The
# properties list is 200 rows: captured whole it is a 20 000-pixel strip nobody can look at, and the top of
# the page is what the manual is pointing at anyway.
SHOTS = [
    ("painel.png", "/", False),
    ("imoveis-filtros.png", "/properties", False),
    ("mediadoras.png", "/agencies", False),
    ("fontes.png", "/sources", True),
    ("configuracoes.png", "/settings", True),
    ("cobertura.png", "/findings", True),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if pathlib.Path(candidate).exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("chromium") or shutil.which("google-chrome")
    if found:
        return found
    sys.exit("no Chrome or Edge found; install one or edit CHROME_CANDIDATES")


class Browser:
    """The thinnest CDP client that does the job: one websocket, one page, synchronous."""

    def __init__(self, port: int):
        self.next_id = 0
        # The PAGE target, not the browser one. `/json/version` hands out the browser-level socket, which
        # has no Page or Runtime domain at all ("'Page.enable' wasn't found").
        self._cm = connect(self._wait_for_page(port), max_size=64 * 1024 * 1024)
        self.ws = self._cm.__enter__()

    def close(self) -> None:
        self._cm.__exit__(None, None, None)

    @staticmethod
    def _wait_for_page(port: int, timeout: float = 30.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as reply:
                    pages = [t for t in json.load(reply) if t.get("type") == "page"]
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001 - the browser is still starting
                pass
            time.sleep(0.3)
        sys.exit("Chrome did not expose a page to drive")

    def send(self, method: str, **params):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != self.next_id:
                continue  # an unrelated event; this client subscribes to none, so just skip it
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str, *, await_promise: bool = False):
        result = self.send("Runtime.evaluate", expression=expression, awaitPromise=await_promise,
                           returnByValue=True)
        return result.get("result", {}).get("value")

    def goto(self, url: str) -> None:
        self.send("Page.navigate", url=url)
        # `load` is not enough on its own here: the theme script and the fonts settle a moment later, and a
        # screenshot taken too early shows an unstyled page - which is exactly the kind of image that makes
        # a manual look broken.
        for _ in range(100):
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.1)
        time.sleep(0.6)

    def screenshot(self, path: pathlib.Path, *, full: bool, attempts: int = 3) -> int:
        """Chrome occasionally answers "Cannot take screenshot with 0 width" right after a navigation,
        when the layout has not settled. It is transient, and losing the whole run to it means the manual
        ends up with a half-captured set of images."""
        params = {"format": "png"}
        if full:
            params["captureBeyondViewport"] = True
        for attempt in range(attempts):
            try:
                data = self.send("Page.captureScreenshot", **params)["data"]
                break
            except RuntimeError:
                if attempt == attempts - 1:
                    raise
                time.sleep(1.0)
        path.write_bytes(base64.b64decode(data))
        return path.stat().st_size


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else "https://andreia-imoveis.duckdns.org").rstrip("/")
    password = os.environ.get("PANEL_PASSWORD") or os.environ.get("SMOKE_PASSWORD")
    if not password:
        sys.exit("set PANEL_PASSWORD (or SMOKE_PASSWORD) to the panel password")
    username = os.environ.get("PANEL_USERNAME", "andreia")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    page = None
    port, profile = _free_port(), tempfile.mkdtemp(prefix="manual-shots-")
    chrome = subprocess.Popen([
        _chrome(), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        f"--window-size={WIDTH},{HEIGHT}", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page = Browser(port)
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT, deviceScaleFactor=1,
                  mobile=False)
        # Headless Chrome reports `prefers-color-scheme: dark`, so without this every image comes out in
        # the dark theme. The manual is printed and read on paper as often as on screen, and light is what
        # the panel shows on a daytime laptop, so pin it - and keep all the images consistent with each
        # other, which matters more than which theme is "right".
        page.send("Emulation.setEmulatedMedia",
                  features=[{"name": "prefers-color-scheme", "value": "light"}])

        # Log in the way she does. `fetch` keeps the cookie in this browser context, and the panel sets it
        # Secure, so an http:// base URL would silently drop it and every page would redirect to /login.
        page.goto(f"{base}/login")
        page.evaluate(
            "fetch('/login', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'},"
            f" body:'username={username}&password={password}', redirect:'manual'}}).then(r => r.status)",
            await_promise=True)
        page.goto(f"{base}/")
        if "/login" in (page.evaluate("location.pathname") or ""):
            sys.exit("login failed: check the password, and use the https:// URL (the cookie is Secure)")

        for name, path, full in SHOTS:
            page.goto(f"{base}{path}")
            title = page.evaluate("document.title") or ""
            size = page.screenshot(OUT_DIR / name, full=full)
            print(f"{name:24} {path:12} {size // 1024:4} KB  {title}")
    finally:
        try:
            if page is not None:
                page.close()
        except Exception:  # noqa: BLE001 - the browser may already be gone
            pass
        chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)
    print(f"\n{len(SHOTS)} images in {OUT_DIR}")


if __name__ == "__main__":
    main()
