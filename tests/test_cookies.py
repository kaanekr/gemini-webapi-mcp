"""_resolve_cookies must take __Secure-1PSID from the exact `.google.com` domain.

browser_cookie3 matches `domain_name` as a substring, so a jar holding cookies
for `.google.com.bd` (or any other ccTLD) also comes back — and a "last wins"
loop would hand Gemini a foreign-domain session (regression: 2026-09-21, every
request answered 1096 / "Are you signed in?").

The chain now also looks at a cookie store and at Chrome DevTools, so these
tests pin the environment down: fresh store path, no CDP probing, no PSID env.
"""
import os
import sys
from http.cookiejar import Cookie, CookieJar

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gemini_webapi_mcp import cookies as C  # noqa: E402
from gemini_webapi_mcp import server as S  # noqa: E402


def _cookie(name, value, domain):
    return Cookie(0, name, value, None, False, domain, True, domain.startswith("."),
                  "/", True, True, None, False, None, None, {})


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Keep the chain from finding anything outside the test."""
    monkeypatch.delenv("GEMINI_PSID", raising=False)
    monkeypatch.delenv("GEMINI_PSIDTS", raising=False)
    monkeypatch.delenv("GEMINI_CDP_URL", raising=False)
    monkeypatch.setenv("GEMINI_CDP_PORTS", "")          # no port scanning in tests
    monkeypatch.setenv("GEMINI_COOKIE_FILE", str(tmp_path / "cookies.json"))
    monkeypatch.delenv("GEMINI_COOKIE_SOURCE", raising=False)


def test_resolve_cookies_prefers_exact_google_com_domain(monkeypatch):
    import browser_cookie3

    jar = CookieJar()
    jar.set_cookie(_cookie("__Secure-1PSID", "good-psid", ".google.com"))
    jar.set_cookie(_cookie("__Secure-1PSIDTS", "good-ts", ".google.com"))
    jar.set_cookie(_cookie("__Secure-1PSID", "bd-psid", ".google.com.bd"))
    jar.set_cookie(_cookie("__Secure-1PSIDTS", "bd-ts", ".google.com.bd"))
    monkeypatch.setattr(browser_cookie3, "chrome", lambda **kw: jar)

    assert S._resolve_cookies() == ("good-psid", "good-ts")


def test_env_wins_over_store(monkeypatch, tmp_path):
    C.save_store("store-psid", "store-ts")
    monkeypatch.setenv("GEMINI_PSID", "env-psid")
    monkeypatch.setenv("GEMINI_PSIDTS", "env-ts")

    assert C.resolve() == ("env-psid", "env-ts", C.SOURCE_ENV)


def test_store_is_used_when_env_is_absent():
    C.save_store("store-psid", "store-ts")

    psid, psidts, source = C.resolve()
    assert (psid, psidts) == ("store-psid", "store-ts")
    assert source == C.SOURCE_STORE


def test_store_accepts_cookie_names(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text('{"__Secure-1PSID": "raw-psid", "__Secure-1PSIDTS": "raw-ts"}', encoding="utf-8")

    assert C.resolve()[0:2] == ("raw-psid", "raw-ts")


def test_browser_cookie3_failure_falls_through(monkeypatch):
    """A locked DB (RequiresAdminError) must not abort the chain."""
    import browser_cookie3

    class RequiresAdminError(Exception):
        pass

    def boom(**kwargs):
        raise RequiresAdminError("snapshot needs admin")

    monkeypatch.setattr(browser_cookie3, "chrome", boom)
    C.save_store("store-psid", "")

    assert C.resolve()[0] == "store-psid"


def test_missing_cookies_raise_actionable_error(monkeypatch):
    import browser_cookie3

    monkeypatch.setattr(browser_cookie3, "chrome", lambda **kw: CookieJar())

    with pytest.raises(C.CookieError) as excinfo:
        C.resolve()

    message = str(excinfo.value)
    assert "GEMINI_PSID" in message
    assert "--remote-debugging-port" in message


def test_cookie_source_can_be_pinned(monkeypatch):
    monkeypatch.setenv("GEMINI_PSID", "env-psid")
    C.save_store("store-psid", "")
    monkeypatch.setenv("GEMINI_COOKIE_SOURCE", "store")

    assert C.resolve()[0] == "store-psid"


def test_cdp_cookies_are_extracted(monkeypatch):
    """CDP path: chrome-ish /json/version + Storage.getCookies payload."""
    monkeypatch.setenv("GEMINI_CDP_URL", "http://127.0.0.1:9222")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            import json
            return json.dumps({
                "Browser": "Chrome/156.0.0.0",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc",
            }).encode()

    monkeypatch.setattr(C.urllib.request, "urlopen", lambda url, timeout=0: FakeResponse())

    cookies = [
        {"name": "__Secure-1PSID", "value": "cdp-psid", "domain": ".google.com"},
        {"name": "__Secure-1PSIDTS", "value": "cdp-ts", "domain": ".google.com"},
        {"name": "__Secure-1PSID", "value": "other", "domain": ".google.co.uk"},
    ]
    monkeypatch.setattr(C, "_cdp_call", lambda ws_url, method: {"cookies": cookies})

    assert C.resolve()[0:3] == ("cdp-psid", "cdp-ts", C.SOURCE_CDP)
