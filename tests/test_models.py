"""Model registry: alias → backend id resolution and env overrides.

The mapping is data, not code, because Google rotates the ids inside
`x-goog-ext-525001261-jspb`. These tests pin the resolution rules: built-ins
keep working, env aliases/extensions do not need a code change, a raw id is
accepted, and an unknown alias fails loudly instead of silently guessing.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gemini_webapi_mcp import models as M  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GEMINI_MODEL_ALIASES", "GEMINI_CHAT_MODEL", "GEMINI_IMAGE_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_builtin_alias_resolves_to_captured_id():
    resolved = M.resolve("gemini-3.0-flash")
    assert resolved.model_id == "fbb127bbb056c959"
    assert resolved.source == M.SOURCE_BUILTIN


def test_header_matches_browser_shape():
    resolved = M.resolve("gemini-3.0-pro")
    header = resolved.as_library_model()["model_header"][M.HEADER_NAME]
    assert header == '[1,null,null,null,"9d8ca3786ebdfbea",null,null,0,[4],null,null,1]'


def test_alias_lookup_is_case_insensitive():
    assert M.resolve("Gemini-3.0-PRO").model_id == "9d8ca3786ebdfbea"


def test_raw_model_id_is_accepted():
    resolved = M.resolve("0123456789ABCDEF")
    assert resolved.model_id == "0123456789abcdef"
    assert resolved.source == M.SOURCE_RAW


def test_env_alias_extends_registry(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_ALIASES", json.dumps({"g4-argon": "aaaaaaaaaaaaaaaa"}))
    resolved = M.resolve("g4-argon")
    assert resolved.model_id == "aaaaaaaaaaaaaaaa"
    assert resolved.source == M.SOURCE_ENV


def test_env_alias_accepts_object_with_caps(monkeypatch):
    monkeypatch.setenv(
        "GEMINI_MODEL_ALIASES",
        json.dumps({"img-pro": {"id": "bbbbbbbbbbbbbbbb", "caps": [4, 5, 6, 8]}}),
    )
    resolved = M.resolve("img-pro")
    assert resolved.caps == [4, 5, 6, 8]
    assert "[4, 5, 6, 8]" in resolved.header()


def test_env_alias_overrides_builtin(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_ALIASES", json.dumps({"gemini-3.0-flash": "cccccccccccccccc"}))
    assert M.resolve("gemini-3.0-flash").model_id == "cccccccccccccccc"


def test_invalid_env_entries_are_skipped(monkeypatch):
    monkeypatch.setenv(
        "GEMINI_MODEL_ALIASES",
        json.dumps({"bad": "not-an-id", "worse": {"id": "ZZZZ", "caps": "nope"}, "good": "dddddddddddddddd"}),
    )
    catalogue = M.available()
    assert "bad" not in catalogue
    assert "worse" not in catalogue
    assert catalogue["good"]["id"] == "dddddddddddddddd"


def test_broken_json_is_ignored(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_ALIASES", "{not json")
    assert M.resolve("gemini-3.0-flash").model_id == "fbb127bbb056c959"


def test_unknown_alias_raises_with_known_list():
    with pytest.raises(M.UnknownModelError) as excinfo:
        M.resolve("gemini-9.9-argon")
    message = str(excinfo.value)
    assert "gemini-3.0-flash" in message
    assert "GEMINI_MODEL_ALIASES" in message


def test_defaults_come_from_env(monkeypatch):
    assert M.default_chat_model() == "gemini-3.0-flash"
    assert M.default_image_model() == "gemini-3.0-flash-thinking"

    monkeypatch.setenv("GEMINI_CHAT_MODEL", "gemini-3.0-pro")
    monkeypatch.setenv("GEMINI_IMAGE_MODEL", "gemini-3.0-flash")
    assert M.resolve().alias == "gemini-3.0-pro"
    assert M.resolve(kind="image").alias == "gemini-3.0-flash"


def test_catalogue_reports_provenance(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL_ALIASES", json.dumps({"mine": "eeeeeeeeeeeeeeee"}))
    entries = {entry["alias"]: entry for entry in M.catalogue()}
    assert entries["gemini-3.0-flash"]["source"] == M.SOURCE_BUILTIN
    assert entries["mine"]["source"] == M.SOURCE_ENV
    assert entries["mine"]["model_id"] == "eeeeeeeeeeeeeeee"


def test_current_web_mode_aliases_are_registered():
    """Ids captured 2026-11-03 from the browser's processSession payload (1.1.2).

    The capture is self-checking: "3.8 Flash" has to resolve to the id this
    project already uses for the fast browser image route.
    """
    expected = {
        "gemini-3.5-flash-lite": "8c46e95b1a07cecc",
        "gemini-3.8-flash": "56fdd199312815e2",
        "gemini-3.1-pro": "e6fa609c3fa255c0",
        "gemini-4-argon": "95a4c0abcba24364",
    }
    for alias, model_id in expected.items():
        assert M.resolve(alias).model_id == model_id

    assert M.resolve("gemini-3.8-flash").model_id == "56fdd199312815e2"

