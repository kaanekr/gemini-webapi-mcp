"""Browser transport — drive the real Gemini tab over Chrome DevTools Protocol.

Why this exists: the library talks to the legacy ``StreamGenerate`` endpoint,
which Google is phasing out. It intermittently serves a text-only variant that
answers attachment/identity requests with "I only generate text", and it stalls
before the first token often enough to look broken. The browser uses the new
``processSession`` transport, which cannot be reproduced from Python: the
prompt is not sent as plain text, it travels inside a sealed blob produced by
the page's own JavaScript (verified by replaying a captured request: the marker
string was absent from the 1.9KB protobuf payload).

So instead of re-implementing the seal, this module uses the seal: it drives a
real tab over CDP and reads the answer from the DOM. Same backend, same model
selection, same file handling as the user sees.

Enable with ``GEMINI_TRANSPORT=browser``. Requires Chrome with
``--remote-debugging-port`` (auto-detected from ``DevToolsActivePort``) or an
explicit ``GEMINI_CDP_URL``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("gemini_mcp")

GEMINI_URL_PREFIX = "https://gemini.google.com"

# UI labels of the mode picker, keyed by the alias the caller uses. The registry
# ids stay the source of truth for the legacy transport; here we need what the
# picker says, because clicking is how the browser picks a model.
MODE_LABELS = {
    "gemini-3.5-flash-lite": "3.5 Flash-Lite",
    "gemini-3.8-flash": "3.8 Flash",
    "gemini-3.1-pro": "3.1 Pro",
    "gemini-4-argon": "4 Argon",
}

ANSWER_SELECTOR = ".model-response-text"
COMPOSER_SELECTOR = '.ql-editor[contenteditable="true"]'


class BrowserTransportError(RuntimeError):
    """Raised when the browser transport cannot complete the request."""


def _devtools_url() -> str:
    explicit = os.environ.get("GEMINI_CDP_URL", "").strip()
    if explicit:
        base = explicit.rstrip("/")
        return base if base.startswith("ws") else base + "/devtools/browser"

    port_file = Path(os.environ.get("CHROME_USER_DATA", "")) / "DevToolsActivePort"
    candidates = [port_file] if str(port_file) != "DevToolsActivePort" else []
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort")
    for path in candidates:
        try:
            port, ws_path = path.read_text(encoding="utf-8").split()[:2]
        except (OSError, ValueError):
            continue
        return f"ws://127.0.0.1:{port}{ws_path}"
    raise BrowserTransportError(
        "Chrome DevTools endpoint not found. Start Chrome with "
        "--remote-debugging-port=9222 (then the port file appears) or set GEMINI_CDP_URL."
    )


class _Cdp:
    """Minimal flattened-session CDP client."""

    def __init__(self, url: str, connect_timeout: float = 60.0, attempts: int = 3):
        from websocket import create_connection

        last: Exception | None = None
        for attempt in range(attempts):
            try:
                self._ws = create_connection(url, timeout=connect_timeout, suppress_origin=True)
                break
            except Exception as exc:  # Chrome's DevTools socket is periodically slow
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(2 + attempt * 2)
        else:
            raise BrowserTransportError(
                f"cannot reach the Chrome DevTools endpoint ({type(last).__name__}: {last}). "
                "Is Chrome still running with --remote-debugging-port?"
            )
        self._id = 0

    def cmd(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._ws.send(json.dumps(msg))
        while True:
            raw = self._ws.recv()
            if not raw:
                raise BrowserTransportError("DevTools socket closed")
            data = json.loads(raw)
            if data.get("id") == self._id:
                if "error" in data:
                    raise BrowserTransportError(data["error"].get("message", "CDP error"))
                return data.get("result", {})

    def js(self, session: str, expression: str, await_promise: bool = False):
        out = self.cmd(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
            session,
        )
        if "exceptionDetails" in out:
            raise BrowserTransportError(f"page script failed: {out['exceptionDetails'].get('text', 'unknown')}")
        return out.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def _open_tab(cdp: _Cdp) -> str:
    """Reuse a Gemini tab when one is open, otherwise create it."""
    targets = cdp.cmd("Target.getTargets", {}).get("targetInfos", [])
    page = next(
        (t for t in targets if t.get("type") == "page" and str(t.get("url", "")).startswith(GEMINI_URL_PREFIX)),
        None,
    )
    if page:
        return page["targetId"]
    created = cdp.cmd("Target.createTarget", {"url": f"{GEMINI_URL_PREFIX}/app"})
    time.sleep(5)
    return created["targetId"]


def _select_mode(cdp: _Cdp, session: str, label: str) -> str:
    """Click a mode in the picker. Returns a short status string."""
    script = """
    (async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      const want = %s;
      let picker = null;
      for (let i = 0; i < 24; i++) {
        picker = [...document.querySelectorAll('button')]
          .find(b => (b.getAttribute('aria-label') || '').startsWith('Open mode picker'));
        if (picker) break;
        await sleep(500);
      }
      if (!picker) return 'no picker';
      if ((picker.getAttribute('aria-label') || '').includes(want)) return 'already ' + want;
      if (!document.querySelector('[role="menu"]')) { picker.click(); await sleep(900); }
      const items = [...document.querySelectorAll('[role="menu"] [role="menuitem"]')];
      const opt = items.find(i => (i.innerText || '').trim().startsWith(want));
      if (!opt) return 'mode not offered: ' + items.map(i => (i.innerText || '').trim().slice(0, 18)).join(' | ');
      opt.click();
      await sleep(1200);
      return 'selected ' + want;
    })()
    """ % json.dumps(label)
    return cdp.js(session, script, await_promise=True)


FIND_FILE_INPUT_JS = """
(() => {
  const walk = (root) => {
    const direct = root.querySelector ? root.querySelector('input[type=file]') : null;
    if (direct) return direct;
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const found = walk(el.shadowRoot);
        if (found) return found;
      }
    }
    return null;
  };
  return walk(document);
})()
"""


def _attach_files(cdp: _Cdp, session: str, files: list[str]) -> str:
    """Push files into the composer input, waiting for the composer to render."""
    object_id = None
    for _ in range(24):
        out = cdp.cmd(
            "Runtime.evaluate",
            {"expression": FIND_FILE_INPUT_JS, "returnByValue": False, "userGesture": True},
            session,
        )
        object_id = out.get("result", {}).get("objectId")
        if object_id:
            break
        time.sleep(0.5)
    if not object_id:
        raise BrowserTransportError(
            "composer file input not found — the tab is not on a loaded Gemini chat "
            "(open gemini.google.com/app, or retry once the page has rendered)"
        )
    cdp.cmd("DOM.setFileInputFiles", {"files": files, "objectId": object_id}, session)
    # Give the app time to upload and render the attachment chips.
    time.sleep(min(10.0, 3.0 + len(files) * 2))
    return "attached"


def _send_prompt(cdp: _Cdp, session: str, prompt: str) -> int:
    """Type and submit. Returns the number of answers already on screen."""
    script = """
    (async () => {
      const sleep = ms => new Promise(r => setTimeout(r, ms));
      let ed = document.querySelector(%s);
      for (let i = 0; i < 24 && !ed; i++) { await sleep(500); ed = document.querySelector(%s); }
      if (!ed) return -1;
      ed.focus();
      document.execCommand('insertText', false, %s);
      await sleep(400);
      const send = [...document.querySelectorAll('button')]
        .find(b => (b.getAttribute('aria-label') || '') === 'Send message');
      if (!send) return -2;
      const before = document.querySelectorAll(%s).length;
      send.click();
      return before;
    })()
    """ % (
        json.dumps(COMPOSER_SELECTOR),
        json.dumps(COMPOSER_SELECTOR),
        json.dumps(prompt),
        json.dumps(ANSWER_SELECTOR),
    )
    before = cdp.js(session, script, await_promise=True)
    if before == -1:
        raise BrowserTransportError("prompt box not found (UI changed, or the tab is not on Gemini)")
    if before == -2:
        raise BrowserTransportError("send button not found (UI changed?)")
    return before


def _wait_answer(
    cdp: _Cdp,
    session: str,
    before: int,
    timeout: float,
    wake=None,
    wake_after: float = 12.0,
) -> str:
    """Poll until the new answer stops growing and no stop button is visible.

    A background tab is throttled by Chrome: timers and rendering slow down, the
    stream can look silent even though the server is done. `wake` is called once
    when nothing has appeared after `wake_after` seconds, so the caller can bring
    the tab to front as a fallback.
    """
    probe = """
    () => {
      const nodes = document.querySelectorAll(%s);
      const stop = [...document.querySelectorAll('button')]
        .some(b => (b.getAttribute('aria-label') || '').toLowerCase().includes('stop'));
      const last = nodes.length ? nodes[nodes.length - 1].innerText.trim() : '';
      return JSON.stringify({n: nodes.length, stop: stop, last: last});
    }
    """ % json.dumps(ANSWER_SELECTOR)

    started = time.time()
    deadline = started + timeout
    previous = None
    stable = 0
    woke = False
    while time.time() < deadline:
        raw = cdp.js(session, f"({probe})()")
        state = raw if isinstance(raw, dict) else json.loads(raw)
        if state["n"] > before and state["last"]:
            if state["last"] == previous:
                stable += 1
            else:
                stable = 0
                previous = state["last"]
            if not state["stop"] and stable >= 1:
                return state["last"]
        elif wake and not woke and (time.time() - started) > wake_after:
            logger.info("browser transport: no output yet — bringing the tab to front")
            woke = True
            try:
                wake()
            except Exception as exc:
                logger.warning("browser transport: could not activate the tab — %s", exc)
        time.sleep(0.7)
    if previous:
        return previous  # timed out but we did get text; better than nothing
    raise BrowserTransportError(f"no answer within {timeout:.0f}s")


_STATE: dict = {"cdp": None, "session": None, "target": None}


def _reset_state() -> None:
    cdp = _STATE.get("cdp")
    if cdp is not None:
        try:
            cdp.close()
        except Exception:
            pass
    _STATE.update({"cdp": None, "session": None, "target": None})


def _session():
    """Reuse one CDP connection for the process lifetime.

    Opening a fresh browser-level WebSocket per call makes Chrome (and any
    policy in front of it) prompt repeatedly, and costs a handshake each time.
    """
    cdp = _STATE.get("cdp")
    if cdp is not None:
        try:
            cdp.js(_STATE["session"], "1 + 1")
            return cdp, _STATE["session"], _STATE["target"]
        except Exception as exc:
            logger.info("browser transport: reconnecting (%s)", type(exc).__name__)
            _reset_state()

    cdp = _Cdp(_devtools_url(), connect_timeout=20, attempts=2)
    try:
        target = _open_tab(cdp)
        session = cdp.cmd("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        cdp.cmd("Runtime.enable", {}, session)
        for method, params in (
            ("Page.enable", {}),
            ("Page.setWebLifecycleState", {"state": "active"}),
            ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ):
            try:
                cdp.cmd(method, params, session)
            except Exception as exc:
                logger.debug("browser transport: %s unavailable (%s)", method, exc)
    except Exception:
        cdp.close()
        raise

    _STATE.update({"cdp": cdp, "session": session, "target": target})
    return cdp, session, target


def _current_cid(cdp: _Cdp, session: str) -> str | None:
    """Conversation id from the tab URL (/app/<cid>), if it already has one."""
    url = cdp.js(session, "location.href") or ""
    return cid_from_url(url)


def cid_from_url(url: str) -> str | None:
    match = re.search(r"/app/([0-9a-f]{8,})", url or "")
    return match.group(1) if match else None


def _goto_conversation(cdp: _Cdp, session: str, cid: str | None) -> None:
    """Open a specific conversation, or a fresh chat when cid is None."""
    target_url = f"{GEMINI_URL_PREFIX}/app/{cid}" if cid else f"{GEMINI_URL_PREFIX}/app"
    current = cdp.js(session, "location.href") or ""
    if current.rstrip("/") == target_url.rstrip("/"):
        return
    cdp.cmd("Page.navigate", {"url": target_url}, session)
    for _ in range(40):
        if cdp.js(session, f"!!document.querySelector({json.dumps(COMPOSER_SELECTOR)})"):
            return
        time.sleep(0.5)
    raise BrowserTransportError(f"conversation did not load: {target_url}")


def ask_chat_sync(
    prompt: str,
    cid: str | None = None,
    files: list[str] | None = None,
    mode_label: str | None = None,
    timeout: float = 300.0,
    new_chat: bool = False,
) -> tuple[str, str | None]:
    """Send a prompt into a conversation; return (answer, conversation_id).

    ``cid`` targets an existing conversation, ``new_chat`` opens a fresh one.
    With neither, the tab is left where it is.
    """
    try:
        cdp, session, target = _session()
        if cid or new_chat:
            _goto_conversation(cdp, session, cid)

        if mode_label:
            logger.info("browser transport: %s", _select_mode(cdp, session, mode_label))
        if files:
            logger.info("browser transport: %s", _attach_files(cdp, session, files))

        def wake() -> None:
            cdp.cmd("Target.activateTarget", {"targetId": target})
            cdp.cmd("Page.bringToFront", {}, session)

        if (os.environ.get("GEMINI_BROWSER_FOCUS", "0") or "0").strip().lower() in ("1", "true", "yes"):
            wake()

        before = _send_prompt(cdp, session, prompt)
        text = _wait_answer(cdp, session, before, timeout, wake=wake)
        return text, _current_cid(cdp, session)
    except Exception:
        _reset_state()
        raise


def ask_sync(
    prompt: str,
    files: list[str] | None = None,
    mode_label: str | None = None,
    timeout: float = 300.0,
) -> str:
    """Send one prompt in the browser and return the answer text."""
    return ask_chat_sync(prompt, files=files, mode_label=mode_label, timeout=timeout)[0]


async def ask(
    prompt: str,
    files: list[str] | None = None,
    model_alias: str | None = None,
    timeout: float | None = None,
) -> str:
    """Async wrapper: the CDP client is blocking, so run it off the event loop."""
    limit = float(os.environ.get("GEMINI_GEN_TIMEOUT", "600")) if timeout is None else timeout
    label = MODE_LABELS.get((model_alias or "").strip().lower())
    return await asyncio.to_thread(ask_sync, prompt, files, label, limit)


async def ask_chat(
    prompt: str,
    cid: str | None = None,
    files: list[str] | None = None,
    model_alias: str | None = None,
    timeout: float | None = None,
    new_chat: bool = False,
) -> tuple[str, str | None]:
    """Async multi-turn variant: returns (answer, conversation_id)."""
    limit = float(os.environ.get("GEMINI_GEN_TIMEOUT", "600")) if timeout is None else timeout
    label = MODE_LABELS.get((model_alias or "").strip().lower())
    return await asyncio.to_thread(ask_chat_sync, prompt, cid, files, label, limit, new_chat)


def probe() -> dict:
    """Is a browser transport usable right now? (used by gemini_models)"""
    try:
        url = _devtools_url()
    except BrowserTransportError as exc:
        return {"available": False, "reason": str(exc)}
    try:
        cdp = _Cdp(url, connect_timeout=8, attempts=2)
    except Exception as exc:
        return {"available": False, "reason": str(exc)}
    try:
        version = cdp.cmd("Browser.getVersion", {})
        return {"available": True, "browser": version.get("product", "?")}
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        cdp.close()
