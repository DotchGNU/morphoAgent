"""Optional language-model assistant for mask review (``morphoagent ui serve`` only).

Privacy
-------
When ``ui.llm.enabled`` is true and the user presses "Ask assistant", the
numbered overlay image of ONE field (downscaled to ``ui.llm.image_width_px``)
and a compact object table are sent to ``ui.llm.endpoint``. They leave the
machine. Do not enable this for unpublished or sensitive data without
permission. There are no automatic or bulk calls, and at most
``ui.llm.max_requests_per_minute`` requests are sent per minute.

Request (``build_request``) and response (``validate_response``) schemas are
documented in docs/ui.md. A provider is a function

``provider(request: dict, settings: LLMConfig, api_key: str | None) -> dict``

that returns a dict with a ``suggestions`` list. Built-in providers:

``generic-json``
    POSTs the request document unchanged as JSON and expects the response
    document as JSON.
``chat-completions``
    Wraps the request in the widely used ``/v1/chat/completions`` message
    shape (served by many self-hosted inference servers) and parses the JSON
    object in the first choice's message content.
``mock``
    Deterministic offline provider for tests and demonstrations.
``package.module:function``
    Any importable function with the provider signature.

Suggestions are advisory only: nothing in this module or the server modifies
the user's edits.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import importlib
import io
import json
import os
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..config import LLMConfig

TASK = "morphoagent.mask_review.v1"
KINDS = ("merged", "fragmented", "not_a_cell")
INSTRUCTIONS = (
    "You review automatic cell segmentation masks on a bright-field (phase-contrast) microscope image. "
    "The image shows the masks as outlines with their label numbers: red solid = kept objects, pale dashed = objects "
    "touching the image frame. The table lists every drawn object with its label, area (px^2 at full resolution), "
    "frame contact (px), aspect ratio and solidity. List only labels that are clearly wrong, each with a short reason "
    "(at most 20 words): 'merged' = one mask covers several cells; 'fragmented' = one cell is split into several masks "
    "(give the other label numbers of the same cell in 'with'); 'not_a_cell' = debris, background or an artefact. "
    "Do not list correct masks. Answer with JSON only: "
    '{"suggestions": [{"label": 12, "kind": "fragmented", "with": [13], "reason": "..."}]}'
)

Provider = Callable[[dict[str, Any], LLMConfig, "str | None"], dict[str, Any]]


class LLMError(RuntimeError):
    """The assistant is not configured, refused the request or returned an unusable answer."""


class RateLimitError(LLMError):
    """More than ``max_requests_per_minute`` requests in the last 60 s."""


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
def overlay_jpeg(overlay: np.ndarray, width: int, quality: int) -> tuple[bytes, int, int]:
    """Downscale an RGB overlay to ``width`` px (never enlarge) and JPEG-encode it; returns (bytes, w, h)."""
    pil = Image.fromarray(overlay).convert("RGB")
    if pil.width > width:
        pil = pil.resize((int(width), int(round(pil.height * width / pil.width))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=int(quality))
    return buf.getvalue(), pil.width, pil.height


def object_table(objects: list[dict[str, Any]], max_rows: int) -> list[dict[str, Any]]:
    """Compact object table sent to the assistant (drawn objects only, at most ``max_rows``)."""
    keep = [o for o in objects if o.get("group_rule") in ("kept", "frame_contact")]
    return [{"label": o["label"], "area_px": o["area_px"], "frame_contact_px": o["border_contact_px"],
             "group": o["group"], "aspect_ratio": o.get("aspect_ratio"), "solidity": o.get("solidity")}
            for o in keep[: int(max_rows)]]


def build_request(image_id: str, jpeg: bytes, width: int, height: int, table: list[dict[str, Any]],
                  settings: LLMConfig) -> dict[str, Any]:
    """The vendor-neutral request document (schema ``morphoagent.mask_review.v1``, see docs/ui.md)."""
    return {
        "task": TASK,
        "model": settings.model,
        "instructions": INSTRUCTIONS,
        "field": image_id,
        "image": {"mime_type": "image/jpeg", "width": int(width), "height": int(height),
                  "data_base64": base64.b64encode(jpeg).decode("ascii")},
        "objects": table,
        "response_schema": {"suggestions": [{"label": "int", "kind": "|".join(KINDS), "with": "[int]", "reason": "str"}]},
    }


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: dict[str, Any], api_key: str | None, timeout: float) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise LLMError("ui.llm.endpoint must be an http(s) URL")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URL scheme checked above
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        # never include headers (they carry the key) in the message
        raise LLMError(f"request to the LLM endpoint failed: {type(exc).__name__}: {exc}") from None


def generic_json_provider(request: dict[str, Any], settings: LLMConfig, api_key: str | None) -> dict[str, Any]:
    """POST the request document as JSON; the endpoint answers with the response document."""
    return _post_json(str(settings.endpoint), request, api_key, settings.timeout_s)


def chat_completions_provider(request: dict[str, Any], settings: LLMConfig, api_key: str | None) -> dict[str, Any]:
    """Send the request in the common chat-completions message shape and parse the JSON answer."""
    img = request["image"]
    table = json.dumps(request["objects"], separators=(",", ":"))
    payload = {
        "model": settings.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": request["instructions"]},
            {"role": "user", "content": [
                {"type": "text", "text": f"Field {request['field']}. Object table (JSON): {table}"},
                {"type": "image_url", "image_url": {"url": f"data:{img['mime_type']};base64,{img['data_base64']}"}},
            ]},
        ],
    }
    out = _post_json(str(settings.endpoint), payload, api_key, settings.timeout_s)
    try:
        text = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("unexpected chat-completions response shape") from None
    return parse_json_text(text)


def mock_provider(request: dict[str, Any], settings: LLMConfig, api_key: str | None) -> dict[str, Any]:
    """Offline provider: flags the smallest drawn object as ``not_a_cell`` and the two lowest-solidity ones as ``merged``."""
    objs = sorted(request["objects"], key=lambda o: (o["area_px"] or 0))
    out = []
    if objs:
        out.append({"label": objs[0]["label"], "kind": "not_a_cell", "reason": "smallest drawn object (mock provider)"})
    for o in sorted(request["objects"], key=lambda o: (o.get("solidity") or 1))[:2]:
        if o["label"] != (objs[0]["label"] if objs else None):
            out.append({"label": o["label"], "kind": "merged", "reason": "low solidity (mock provider)"})
    return {"suggestions": out}


BUILTIN: dict[str, Provider] = {
    "generic-json": generic_json_provider,
    "chat-completions": chat_completions_provider,
    "mock": mock_provider,
}


def get_provider(name: str) -> Provider:
    """Built-in provider by name, or ``package.module:function``."""
    if name in BUILTIN:
        return BUILTIN[name]
    if ":" in name:
        mod, _, fn = name.partition(":")
        try:
            return getattr(importlib.import_module(mod), fn)
        except (ImportError, AttributeError) as exc:
            raise LLMError(f"cannot load provider {name!r}: {exc}") from None
    raise LLMError(f"unknown ui.llm.provider {name!r}; use one of {sorted(BUILTIN)} or package.module:function")


def parse_json_text(text: str) -> dict[str, Any]:
    """Parse a JSON object from model text (tolerates a fenced code block or surrounding prose)."""
    s = text.strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise LLMError("the model answer contains no JSON object")
    try:
        return json.loads(s[a : b + 1])
    except ValueError:
        raise LLMError("the model answer is not valid JSON") from None


def validate_response(resp: Any, known_labels: set[int]) -> list[dict[str, Any]]:
    """Keep only well-formed suggestions about labels that exist in the field.

    Returns
    -------
    list of dict
        ``{"label": int, "kind": one of KINDS, "with": [int], "reason": str (<= 300 chars)}``.
    """
    if not isinstance(resp, dict) or not isinstance(resp.get("suggestions"), list):
        raise LLMError("response must be a JSON object with a 'suggestions' list")
    out, seen = [], set()
    for s in resp["suggestions"]:
        if not isinstance(s, dict):
            continue
        try:
            lb = int(s.get("label"))
        except (TypeError, ValueError):
            continue
        kind = str(s.get("kind", "")).strip().lower().replace(" ", "_").replace("-", "_")
        if lb not in known_labels or kind not in KINDS or (lb, kind) in seen:
            continue
        partners = []
        for w in s.get("with") or []:
            try:
                w = int(w)
            except (TypeError, ValueError):
                continue
            if w in known_labels and w != lb:
                partners.append(w)
        seen.add((lb, kind))
        out.append({"label": lb, "kind": kind, "with": partners, "reason": str(s.get("reason", ""))[:300]})
    return out


# --------------------------------------------------------------------------- #
# Cache and rate limit
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Sliding 60-s window of at most ``per_minute`` events (thread-safe)."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = int(per_minute)
        self.clock = clock
        self._t: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Record one request or raise :class:`RateLimitError`."""
        with self._lock:
            now = self.clock()
            while self._t and now - self._t[0] >= 60.0:
                self._t.popleft()
            if len(self._t) >= self.per_minute:
                wait = 60.0 - (now - self._t[0])
                raise RateLimitError(f"rate limit of {self.per_minute} request(s) per minute reached; retry in {wait:.0f} s")
            self._t.append(now)


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON to ``path`` through a temporary file in the same folder and ``os.replace``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class SuggestionCache:
    """JSON file cache of validated suggestions keyed by a hash of the request."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @staticmethod
    def key(request: dict[str, Any], settings: LLMConfig) -> str:
        """SHA-256 of provider, endpoint, model and the full request document."""
        h = hashlib.sha256()
        h.update(json.dumps([settings.provider, settings.endpoint, settings.model], sort_keys=True).encode())
        h.update(json.dumps(request, sort_keys=True, separators=(",", ":")).encode())
        return h.hexdigest()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def get(self, key: str) -> list[dict[str, Any]] | None:
        with self._lock:
            hit = self._load().get(key)
        return None if hit is None else hit["suggestions"]

    def put(self, key: str, field: str, suggestions: list[dict[str, Any]]) -> None:
        with self._lock:
            data = self._load()
            data[key] = {"field": field, "saved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "suggestions": suggestions}
            atomic_write_json(self.path, data)


class Assistant:
    """Request suggestions for one field at a time (cache -> rate limit -> provider -> validation)."""

    def __init__(self, settings: LLMConfig, cache_path: Path, provider: Provider | None = None) -> None:
        self.settings = settings
        self.provider = provider or get_provider(settings.provider)
        self.cache = SuggestionCache(cache_path)
        self.limiter = RateLimiter(settings.max_requests_per_minute)

    def suggest(self, image_id: str, overlay: np.ndarray, objects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """Return (suggestions, from_cache) for one field."""
        s = self.settings
        if not s.enabled:
            raise LLMError("the assistant is disabled (ui.llm.enabled: false)")
        jpeg, w, h = overlay_jpeg(overlay, s.image_width_px, s.jpeg_quality)
        req = build_request(image_id, jpeg, w, h, object_table(objects, s.max_objects), s)
        key = self.cache.key(req, s)
        hit = self.cache.get(key)
        if hit is not None:
            return hit, True
        self.limiter.acquire()
        api_key = os.environ.get(s.api_key_env) if s.api_key_env else None
        resp = self.provider(req, s, api_key or None)
        out = validate_response(resp, {int(o["label"]) for o in objects})
        self.cache.put(key, image_id, out)
        return out, False
