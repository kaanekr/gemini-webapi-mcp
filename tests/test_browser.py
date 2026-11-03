"""Browser transport: wiring that can be checked without Chrome.

The CDP calls themselves need a live browser, but the contract with the rest of
the server can be verified: every mode label maps to a registered alias, and a
missing DevTools endpoint fails with an actionable message instead of hanging.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gemini_webapi_mcp import browser as B  # noqa: E402
from gemini_webapi_mcp import models as M  # noqa: E402


def test_every_mode_label_is_a_registered_alias():
    known = set(M.available())
    assert set(B.MODE_LABELS) <= known


def test_mode_labels_match_the_picker_wording():
    # The picker renders these exact strings; a rename here silently stops model
    # selection, so pin them.
    assert B.MODE_LABELS["gemini-4-argon"] == "4 Argon"
    assert B.MODE_LABELS["gemini-3.8-flash"] == "3.8 Flash"
    assert B.MODE_LABELS["gemini-3.1-pro"] == "3.1 Pro"
    assert B.MODE_LABELS["gemini-3.5-flash-lite"] == "3.5 Flash-Lite"


def test_devtools_url_comes_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_CDP_URL", "http://127.0.0.1:9333")
    assert B._devtools_url() == "http://127.0.0.1:9333/devtools/browser"


def test_devtools_url_passes_ws_through(monkeypatch):
    monkeypatch.setenv("GEMINI_CDP_URL", "ws://127.0.0.1:9333/devtools/browser/abc")
    assert B._devtools_url() == "ws://127.0.0.1:9333/devtools/browser/abc"


def test_missing_endpoint_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_CDP_URL", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))          # no Chrome profile here
    monkeypatch.setenv("CHROME_USER_DATA", str(tmp_path))
    with pytest.raises(B.BrowserTransportError) as excinfo:
        B._devtools_url()
    assert "--remote-debugging-port" in str(excinfo.value)


def test_probe_reports_unavailable_instead_of_raising(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_CDP_URL", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("CHROME_USER_DATA", str(tmp_path))
    result = B.probe()
    assert result["available"] is False
    assert "reason" in result


def test_cid_from_url():
    assert B.cid_from_url("https://gemini.google.com/app/414d988c96ebd5b7") == "414d988c96ebd5b7"
    assert B.cid_from_url("https://gemini.google.com/app/414d988c96ebd5b7?hl=en") == "414d988c96ebd5b7"
    assert B.cid_from_url("https://gemini.google.com/app") is None
    assert B.cid_from_url("") is None
    assert B.cid_from_url("https://gemini.google.com/other/abc") is None
