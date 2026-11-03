"""Model registry: alias -> backend model id, with environment overrides.

``gemini_webapi`` ships a closed ``Model`` enum, and Google keeps rotating the
ids that travel inside the ``x-goog-ext-525001261-jspb`` header. The Gemini web
UI advertises names ("3.8 Flash", "3.1 Pro", "4 Argon", ...) that have no
counterpart in the library, and those ids are not published anywhere — so this
module treats the mapping as *data* instead of code:

* the three built-in aliases keep behaving exactly as before;
* ``GEMINI_MODEL_ALIASES`` adds or overrides aliases without patching anything::

      GEMINI_MODEL_ALIASES='{"g4-argon": "0123456789abcdef"}'
      GEMINI_MODEL_ALIASES='{"g4-argon": {"id": "0123456789abcdef", "caps": [4,5,6,8]}}'

* a bare 16-hex model id is accepted directly::

      gemini_chat(prompt="hi", model="0123456789abcdef")

Nothing here invents an id. An unknown alias raises a list of what *is* known,
so a wrong guess fails loudly instead of silently serving another model.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("gemini_mcp")

HEADER_NAME = "x-goog-ext-525001261-jspb"
# Browser shape captured from a real StreamGenerate request. `{id}` is the model
# id, `{caps}` the capability array (JSON, e.g. [4] or [4,5,6,8]).
HEADER_TEMPLATE = '[1,null,null,null,"{id}",null,null,0,{caps},null,null,1]'

# Ids are 16 lowercase hex characters (e.g. "fbb127bbb056c959").
_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Defaults, overridable through env without touching the code.
DEFAULT_CHAT_ALIAS = "gemini-3.0-flash"
DEFAULT_IMAGE_ALIAS = "gemini-3.0-flash-thinking"

# Captured from gemini_webapi.constants.Model. Kept here so an id can be
# overridden per alias through GEMINI_MODEL_ALIASES without editing the pin.
BUILTIN_ALIASES: dict[str, dict] = {
    "gemini-3.0-flash": {
        "id": "fbb127bbb056c959",
        "caps": [4],
        "label": "Gemini 3.0 Flash",
        "note": "fast chat / file and URL analysis",
    },
    "gemini-3.0-pro": {
        "id": "9d8ca3786ebdfbea",
        "caps": [4],
        "label": "Gemini 3.0 Pro",
        "note": "heavier reasoning",
    },
    "gemini-3.0-flash-thinking": {
        "id": "5bf011840784117a",
        "caps": [4],
        "label": "Gemini 3.0 Flash Thinking",
        "note": "image generation default (header is remapped to GEMINI_IMAGE_MODEL_ID)",
    },
}

SOURCE_BUILTIN = "builtin"
SOURCE_ENV = "env"
SOURCE_RAW = "raw-id"


class UnknownModelError(ValueError):
    """Raised when a model alias cannot be resolved to a backend id."""

    def __init__(self, name: str, known: list[str]):
        self.name = name
        self.known = known
        super().__init__(
            f"Unknown model {name!r}. Known aliases: {', '.join(known)}. "
            "Pass a 16-hex model id, or register an alias with "
            'GEMINI_MODEL_ALIASES=\'{"my-alias": "<16-hex-id>"}\'.'
        )


@dataclass(frozen=True)
class ResolvedModel:
    """A model alias bound to a concrete backend id."""

    alias: str
    model_id: str
    caps: list[int] = field(default_factory=lambda: [4])
    label: str = ""
    source: str = SOURCE_BUILTIN

    def header(self) -> str:
        return HEADER_TEMPLATE.format(id=self.model_id, caps=json.dumps(self.caps))

    def as_library_model(self) -> dict:
        """Shape accepted by ``gemini_webapi`` (``Model | str | dict``)."""
        return {"model_name": self.alias, "model_header": {HEADER_NAME: self.header()}}

    def describe(self) -> dict:
        return {
            "alias": self.alias,
            "model_id": self.model_id,
            "caps": self.caps,
            "label": self.label or self.alias,
            "source": self.source,
        }


def _normalise_caps(caps) -> list[int]:
    """Validate the capability array that goes into the model header."""
    if caps is None:
        return [4]
    if isinstance(caps, str):
        try:
            caps = json.loads(caps)
        except json.JSONDecodeError as exc:
            raise ValueError(f"caps is not valid JSON: {exc.msg}") from exc
    if not isinstance(caps, (list, tuple)) or not caps:
        raise ValueError("caps must be a non-empty list of integers")
    out: list[int] = []
    for item in caps:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(f"caps entries must be non-negative integers, got {item!r}")
        out.append(item)
    return out


def _env_aliases() -> dict[str, dict]:
    """Parse ``GEMINI_MODEL_ALIASES``; malformed entries are skipped loudly."""
    raw = os.environ.get("GEMINI_MODEL_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("GEMINI_MODEL_ALIASES ignored — invalid JSON: %s", exc.msg)
        return {}
    if not isinstance(data, dict):
        logger.warning("GEMINI_MODEL_ALIASES ignored — expected a JSON object")
        return {}

    aliases: dict[str, dict] = {}
    for name, spec in data.items():
        try:
            if isinstance(spec, str):
                spec = {"id": spec}
            if not isinstance(spec, dict):
                raise ValueError("expected a string id or an object")
            model_id = str(spec.get("id") or spec.get("model_id") or "").strip().lower()
            if not _ID_RE.match(model_id):
                raise ValueError(f"model id must be 16 lowercase hex chars, got {model_id!r}")
            aliases[str(name)] = {
                "id": model_id,
                "caps": _normalise_caps(spec.get("caps")),
                "label": str(spec.get("label") or name),
                "note": str(spec.get("note") or "registered via GEMINI_MODEL_ALIASES"),
                "source": SOURCE_ENV,
            }
        except ValueError as exc:
            logger.warning("GEMINI_MODEL_ALIASES[%r] ignored — %s", name, exc)
    return aliases


def available() -> dict[str, dict]:
    """All known aliases: built-ins first, then env overrides on top."""
    models = {name: {**spec, "source": SOURCE_BUILTIN} for name, spec in BUILTIN_ALIASES.items()}
    models.update(_env_aliases())
    return models


def default_chat_model() -> str:
    return (os.environ.get("GEMINI_CHAT_MODEL") or DEFAULT_CHAT_ALIAS).strip()


def default_image_model() -> str:
    return (os.environ.get("GEMINI_IMAGE_MODEL") or DEFAULT_IMAGE_ALIAS).strip()


def resolve(name: str | None = None, kind: str = "chat") -> ResolvedModel:
    """Resolve an alias (or raw id) into a :class:`ResolvedModel`.

    Args:
        name: alias, raw 16-hex model id, or ``None`` for the default of ``kind``.
        kind: ``"chat"`` or ``"image"`` — picks the default alias when name is None.
    """
    candidate = (name or (default_image_model() if kind == "image" else default_chat_model())).strip()
    if not candidate:
        candidate = default_image_model() if kind == "image" else default_chat_model()

    lowered = candidate.lower()
    if _ID_RE.match(lowered):
        return ResolvedModel(alias=candidate, model_id=lowered, caps=[4], label=candidate, source=SOURCE_RAW)

    models = available()
    if candidate in models:
        return _from_spec(candidate, models[candidate])
    for alias, spec in models.items():
        if alias.lower() == lowered:
            return _from_spec(alias, spec)

    raise UnknownModelError(candidate, sorted(models))


def _from_spec(alias: str, spec: dict) -> ResolvedModel:
    return ResolvedModel(
        alias=alias,
        model_id=spec["id"],
        caps=list(spec.get("caps") or [4]),
        label=spec.get("label", alias),
        source=spec.get("source", SOURCE_BUILTIN),
    )


def catalogue() -> list[dict]:
    """Every alias with its id and provenance, for the ``gemini_models`` tool."""
    entries = []
    for alias, spec in available().items():
        entries.append(
            ResolvedModel(
                alias=alias,
                model_id=spec["id"],
                caps=list(spec.get("caps") or [4]),
                label=spec.get("label", alias),
                source=spec.get("source", SOURCE_BUILTIN),
            ).describe()
        )
    return entries
