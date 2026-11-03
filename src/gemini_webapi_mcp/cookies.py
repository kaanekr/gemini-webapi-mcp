"""Cookie resolution for the Gemini web app, with Windows-friendly fallbacks.

The upstream project relies on ``browser_cookie3`` reading Chrome's cookie
database directly. On Windows that only works while Chrome is *closed*: Chrome
keeps the ``Cookies`` SQLite file open with an exclusive lock, so
``browser_cookie3`` falls back to a VSS snapshot and raises ``RequiresAdminError``
— the MCP server then dies at startup with "Gemini cookies not found".

This module keeps that path but adds two that do not need elevation or a Chrome
restart:

1. ``GEMINI_PSID`` / ``GEMINI_PSIDTS`` environment variables (explicit).
2. A JSON cookie store — ``GEMINI_COOKIE_FILE`` or
   ``~/.gemini-webapi-mcp/cookies.json``::

       {"psid": "g.a000...", "psidts": "sidts-..."}

   Write it once (``GEMINI_COOKIE_STORE_WRITE=1`` makes the server do it on the
   next successful read) and the server keeps working across Chrome upgrades.
3. ``browser_cookie3`` — the upstream behaviour, still first choice when Chrome
   is not running.
4. Chrome DevTools Protocol — if Chrome was started with
   ``--remote-debugging-port=9222`` (or ``GEMINI_CDP_URL`` is set), cookies are
   fetched over CDP with ``Storage.getCookies``. This works while Chrome is
   running and needs no admin rights.

``GEMINI_COOKIE_SOURCE`` pins one source: ``env``, ``store``, ``browser``,
``cdp`` or ``auto`` (default, walks the chain above).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("gemini_mcp")

PSID_NAMES = ("__Secure-1PSID", "psid", "secure_1psid", "GEMINI_PSID")
PSIDTS_NAMES = ("__Secure-1PSIDTS", "psidts", "secure_1psidts", "GEMINI_PSIDTS")

GOOGLE_DOMAINS = (".google.com", "google.com")

SOURCE_ENV = "environment variables"
SOURCE_STORE = "cookie store"
SOURCE_BROWSER = "Chrome cookies (browser-cookie3)"
SOURCE_CDP = "Chrome DevTools Protocol"


class CookieError(RuntimeError):
    """Raised when no usable Gemini cookies could be found."""


def store_path() -> Path:
    """Location of the JSON cookie store."""
    explicit = os.environ.get("GEMINI_COOKIE_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".gemini-webapi-mcp" / "cookies.json"


# ---------------------------------------------------------------------------
# 1. Environment variables
# ---------------------------------------------------------------------------

def _from_env() -> tuple[str, str] | None:
    psid = os.environ.get("GEMINI_PSID", "").strip()
    if not psid:
        return None
    return psid, os.environ.get("GEMINI_PSIDTS", "").strip()


# ---------------------------------------------------------------------------
# 2. On-disk store
# ---------------------------------------------------------------------------

def _pick(mapping: dict, names: tuple[str, ...]) -> str:
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _from_store() -> tuple[str, str] | None:
    path = store_path()
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cookie store %s unreadable — %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("cookie store %s ignored — expected a JSON object", path)
        return None

    psid = _pick(data, PSID_NAMES)
    if not psid:
        logger.warning("cookie store %s has no __Secure-1PSID", path)
        return None
    return psid, _pick(data, PSIDTS_NAMES)


def save_store(psid: str, psidts: str = "") -> Path:
    """Persist cookies to :func:`store_path`. File is created user-only where possible."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "psid": psid,
        "psidts": psidts,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": "Secret. Grants access to the signed-in Google account — do not commit or share.",
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    tmp.replace(path)
    try:  # best effort on POSIX; Windows ACLs are inherited from the profile dir
        path.chmod(0o600)
    except OSError:
        pass
    logger.info("cookies stored in %s", path)
    return path


# ---------------------------------------------------------------------------
# 3. browser-cookie3
# ---------------------------------------------------------------------------

def _from_browser() -> tuple[str, str] | None:
    try:
        import browser_cookie3
    except ImportError:
        logger.debug("browser-cookie3 not installed")
        return None

    cookie_file = os.environ.get("CHROME_COOKIE_FILE") or None
    try:
        jar = browser_cookie3.chrome(domain_name=".google.com", cookie_file=cookie_file)
    except Exception as exc:  # RequiresAdminError, locked file, missing profile, ...
        message = type(exc).__name__
        if message == "RequiresAdminError":
            logger.info(
                "Chrome is running and holds its cookie DB locked (%s). "
                "Close Chrome, use a cookie store, or start Chrome with "
                "--remote-debugging-port=9222.",
                message,
            )
        else:
            logger.warning("browser-cookie3 could not read Chrome cookies — %s", message)
        return None

    psid = ""
    psidts = ""
    for cookie in jar:
        # domain_name is a substring filter: `.google.com.bd` also matches, and
        # those session cookies are not valid for gemini.google.com.
        if cookie.domain not in GOOGLE_DOMAINS:
            continue
        if cookie.name == "__Secure-1PSID" and cookie.value:
            psid = cookie.value
        elif cookie.name == "__Secure-1PSIDTS" and cookie.value:
            psidts = cookie.value
    if not psid:
        logger.info("browser-cookie3: no __Secure-1PSID cookie found in Chrome")
        return None
    return psid, psidts


# ---------------------------------------------------------------------------
# 4. Chrome DevTools Protocol
# ---------------------------------------------------------------------------

def _cdp_bases() -> list[str]:
    explicit = os.environ.get("GEMINI_CDP_URL", "").strip()
    if explicit:
        return [explicit.rstrip("/")]
    spec = os.environ.get("GEMINI_CDP_PORTS", "9222-9235").strip()
    ports: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            try:
                ports.extend(range(int(low), int(high) + 1))
            except ValueError:
                continue
        else:
            try:
                ports.append(int(chunk))
            except ValueError:
                continue
    return [f"http://127.0.0.1:{port}" for port in ports]


def _cdp_call(ws_url: str, method: str) -> dict:
    from websocket import create_connection  # lazy: only needed for this path

    ws = create_connection(ws_url, timeout=10, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method}))
        while True:
            raw = ws.recv()
            if not raw:
                raise RuntimeError("DevTools socket closed")
            payload = json.loads(raw)
            if payload.get("id") == 1:
                if "error" in payload:
                    raise RuntimeError(payload["error"].get("message", "CDP error"))
                return payload.get("result", {})
    finally:
        ws.close()


def _extract(cookies: list[dict]) -> tuple[str, str] | None:
    psid = ""
    psidts = ""
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if cookie.get("domain") not in GOOGLE_DOMAINS:
            continue
        name = cookie.get("name")
        value = cookie.get("value") or ""
        if not value:
            continue
        if name == "__Secure-1PSID":
            psid = value
        elif name == "__Secure-1PSIDTS":
            psidts = value
    if not psid:
        return None
    return psid, psidts


def _from_cdp() -> tuple[str, str] | None:
    for base in _cdp_bases():
        try:
            with urllib.request.urlopen(f"{base}/json/version", timeout=1.5) as resp:
                info = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(info, dict) or "Chrome/" not in str(info.get("Browser", "")):
            continue  # port is served by something that is not Chrome
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            continue

        for method in ("Storage.getCookies", "Network.getAllCookies"):
            try:
                result = _cdp_call(ws_url, method)
            except Exception as exc:  # no websocket-client, protocol error, ...
                logger.debug("CDP %s failed on %s — %s", method, base, exc)
                continue
            found = _extract(result.get("cookies") or [])
            if found:
                logger.info("cookies read over CDP from %s", base)
                return found
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(source: str | None = None) -> tuple[str, str, str]:
    """Return ``(psid, psidts, source_label)`` or raise :class:`CookieError`."""
    order = (source or os.environ.get("GEMINI_COOKIE_SOURCE", "auto")).strip().lower()

    chain = {
        "env": [("env", _from_env, SOURCE_ENV)],
        "store": [("store", _from_store, SOURCE_STORE)],
        "browser": [("browser", _from_browser, SOURCE_BROWSER)],
        "cdp": [("cdp", _from_cdp, SOURCE_CDP)],
    }
    if order == "auto":
        steps = [
            ("env", _from_env, SOURCE_ENV),
            ("store", _from_store, SOURCE_STORE),
            ("browser", _from_browser, SOURCE_BROWSER),
            ("cdp", _from_cdp, SOURCE_CDP),
        ]
    elif order in chain:
        steps = chain[order]
    else:
        raise CookieError(
            f"GEMINI_COOKIE_SOURCE={order!r} is not one of: auto, env, store, browser, cdp"
        )

    for name, reader, label in steps:
        try:
            found = reader()
        except Exception as exc:  # a broken source must not kill the chain
            logger.warning("cookie source %s failed — %s: %s", name, type(exc).__name__, exc)
            continue
        if found and found[0]:
            psid, psidts = found[0], found[1] if len(found) > 1 else ""
            maybe_persist(psid, psidts, label)
            return psid, psidts, label

    raise CookieError(_help_text())


def _help_text() -> str:
    path = store_path()
    return (
        "Gemini cookies not found. Pick one:\n"
        "  1. Point GEMINI_PSID / GEMINI_PSIDTS at the values from "
        "gemini.google.com → DevTools → Application → Cookies.\n"
        f"  2. Write them once to {path} as "
        '{"psid": "...", "psidts": "..."} — the server also saves there '
        "automatically when GEMINI_COOKIE_STORE_WRITE=1.\n"
        "  3. Close Chrome completely and start the server (browser-cookie3 can "
        "only read the DB while Chrome is not holding it locked; on Windows a "
        "running Chrome makes it require admin rights).\n"
        "  4. Start Chrome with --remote-debugging-port=9222 and restart the "
        "server — cookies are then read over the DevTools protocol.\n"
        "Tip: gemini_reset re-runs this chain without restarting the MCP client."
    )


def maybe_persist(psid: str, psidts: str, source_label: str) -> None:
    """Store newly resolved cookies when the user opted in."""
    if os.environ.get("GEMINI_COOKIE_STORE_WRITE", "0") in ("0", "", "false", "False"):
        return
    if source_label == SOURCE_STORE:
        return
    try:
        save_store(psid, psidts)
    except OSError as exc:
        logger.warning("could not write cookie store — %s", exc)
