"""
Playwright smoke tests for the Hexa Finance FastAPI app.

Two modes:

1. Local (default): a fresh FastAPI server is started with
   `python -m uvicorn app.main:app` on a free localhost port, with the DB env
   vars blanked so the app runs without a database.

       python -m pytest

2. Deployed: point the tests at an already-running site via PLAYWRIGHT_BASE_URL;
   no local server is started.

       PLAYWRIGHT_BASE_URL=https://operations.hexamatics.finance/ python -m pytest

Optional authenticated test runs only when PLAYWRIGHT_TEST_EMAIL and
PLAYWRIGHT_TEST_PASSWORD are set. Never hardcode real credentials or secrets.
"""
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_URL = os.environ.get("PLAYWRIGHT_BASE_URL", "").strip()


# ── helpers ──────────────────────────────────────────────────────────────────
def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _wait_for_health(base: str, proc=None, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False  # server process exited early
        try:
            with urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (URLError, ConnectionError, OSError):
            pass
        time.sleep(0.3)
    return False


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def base_url():
    """Deployed URL if PLAYWRIGHT_BASE_URL is set, else a freshly started local
    uvicorn server with the DB env vars blanked."""
    if DEPLOYED_URL:
        yield DEPLOYED_URL.rstrip("/")
        return

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "APP_URL": url,
        # Run without a database so the app boots and the login error path works.
        "DATABASE_URL": "",
        "SUPABASE_URL": "",
        "SUPABASE_SERVICE_KEY": "",
        "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    log = tempfile.TemporaryFile(mode="w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_for_health(url, proc=proc, timeout=40):
            proc.terminate()
            log.seek(0)
            raise RuntimeError("uvicorn did not become healthy:\n" + log.read())
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(browser, base_url) -> Page:
    context = browser.new_context(base_url=base_url, ignore_https_errors=True)
    context.set_default_timeout(15000)
    pg = context.new_page()
    try:
        yield pg
    finally:
        context.close()


# ── smoke tests ──────────────────────────────────────────────────────────────
def test_health_ok(page: Page):
    resp = page.request.get("/api/health")
    assert resp.status == 200
    body = resp.json()
    assert body.get("status") == "ok"


def test_root_redirects_to_login(page: Page):
    page.goto("/")
    expect(page).to_have_url(re.compile(r"/login"))


def test_login_page_renders(page: Page):
    page.goto("/login")
    expect(page).to_have_title(re.compile("sign in", re.I))
    expect(page.locator("h1.login-title")).to_have_text(re.compile("sign in", re.I))


def test_email_field_visible(page: Page):
    page.goto("/login")
    expect(page.locator("#email")).to_be_visible()


def test_no_password_field(page: Page):
    """Passwordless: the login page must NOT ask for a password."""
    page.goto("/login")
    expect(page.locator("#password")).to_have_count(0)


def test_send_link_button_visible(page: Page):
    page.goto("/login")
    expect(page.get_by_role("button", name=re.compile("sign-in link", re.I))).to_be_visible()


def test_request_link_does_not_crash(page: Page):
    """Requesting a link must not 500. With no DB configured locally the page
    stays on /login and shows the 'Database not configured.' error; against a
    real DB it shows the neutral 'link is on its way' confirmation instead."""
    page.goto("/login")
    page.fill("#email", "nobody@example.com")
    page.get_by_role("button", name=re.compile("sign-in link", re.I)).click()
    expect(page).to_have_url(re.compile(r"/login"))
    expect(page.locator(".error-msg, .success-msg")).to_be_visible()


def test_invalid_verify_token_shows_error(page: Page):
    """A bogus /auth/verify token must not log anyone in — it returns to /login
    with an error and no session."""
    page.goto("/auth/verify?token=deadbeef-not-a-real-token")
    expect(page).to_have_url(re.compile(r"/login|/auth/verify"))
    expect(page.locator(".error-msg")).to_be_visible()
