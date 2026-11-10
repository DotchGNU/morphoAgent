"""Live review server (``morphoagent ui serve``), standard library ``http.server`` only.

Security model
--------------
* Without a token the server only binds to a loopback address (127.0.0.1,
  ::1, localhost): only programs on the same machine can reach it.
* A non-loopback ``ui.host`` requires a shared token in the environment
  variable named by ``ui.token_env``; the server refuses to start otherwise.
  Every request must then carry the token (``X-BFM-Token`` header, or
  ``?token=`` on the first page load). Tokens are compared in constant time
  and are never logged (request lines are logged without query strings).
* Plain HTTP: on a shared network use an SSH tunnel (docs/ui.md).
* Cross-origin requests are refused unless their origin is listed in
  ``ui.cors_origins``.

Decision endpoints (docs/protocol.md): ``GET /api/proposals``,
``POST /api/decide`` (``proposal_id``, ``choice``, ``comment``,
``proposal_sha256``; 409 when already decided or the proposal changed) and
``POST /api/snapshot`` (``proposal_id``, optional ``option_id``, ``comment``).
"""

from __future__ import annotations

import dataclasses
import hmac
import ipaddress
import json
import os
import threading
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
import pandas as pd
import tifffile

from ...records import protocol as records
from ..common import LOG, MissingInputError, label_path, load_gray, read_inventory, require_step
from ..config import PRESET_NAMES, Config
from ..overlay import numbered_overlay
from ..presets import get_preset, preview_metrics
from ..selection import GROUP_FRAME, GROUP_KEPT
from .build import render_page
from .dataset import build_dataset, encode_jpeg, fit_size
from .llm import Assistant, LLMError, RateLimitError, atomic_write_json

STATE_SCHEMA = "morphoagent-ui-state/1"


class UIServerError(RuntimeError):
    """The server cannot start with the given configuration."""


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP addresses."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def resolve_token(host: str, token_env: str | None, environ: dict[str, str] | None = None) -> str | None:
    """Access token from the environment; raise :class:`UIServerError` for a non-loopback host without one."""
    env = os.environ if environ is None else environ
    token = (env.get(token_env) or "").strip() if token_env else ""
    if not is_loopback(host) and not token:
        raise UIServerError(
            f"refusing to listen on {host!r}: a non-loopback address makes the review page reachable from other "
            f"machines, so a shared access token is required. Set the environment variable "
            f"{token_env or '<ui.token_env>'} to a long random string (e.g. "
            "python -c \"import secrets; print(secrets.token_urlsafe(24))\"), or bind to 127.0.0.1 and use an SSH tunnel.")
    if token and len(token) < 16:
        raise UIServerError(f"the token in {token_env} is too short; use at least 16 characters")
    return token or None


def side_file(cfg: Config, configured: str | None, suffix: str) -> Path:
    """Configured path, or ``<config stem><suffix>`` next to the config file (``output_dir`` when there is none)."""
    if configured:
        return Path(configured)
    if cfg.config_path:
        p = Path(cfg.config_path)
        return p.with_name(p.stem + suffix)
    return Path(cfg.output_dir) / f"morphoagent{suffix}"


class UIApp:
    """State shared by all request handlers of one server."""

    def __init__(self, cfg: Config, token: str | None, assistant: Assistant | None = None) -> None:
        self.cfg = cfg
        self.token = token
        self.doc = build_dataset(cfg, live=True)
        self.html = render_page(self.doc).encode("utf-8")
        self.fields = {f["image"]: f for f in self.doc["fields"]}
        try:
            self.inventory = read_inventory(cfg).set_index("image")
        except MissingInputError:  # step 1 not run yet: only the Decisions tab has content
            self.inventory = pd.DataFrame(columns=["path", "condition"], index=pd.Index([], name="image"))
        self.state_path = side_file(cfg, cfg.ui.state_file, "_ui_state.json")
        self.lock = threading.Lock()
        self.assistant = assistant
        if self.assistant is None and cfg.ui.llm.enabled:
            self.assistant = Assistant(cfg.ui.llm, side_file(cfg, cfg.ui.llm.cache_file, "_llm_cache.json"))
        self._gray = lru_cache(maxsize=4)(lambda image: load_gray(self.inventory.loc[image, "path"]))

    # ------------------------------------------------------------- API helpers
    def field_list(self) -> list[dict[str, Any]]:
        """Summary of every field (no image data)."""
        return [{"image": f["image"], "condition": f["condition"], "field": f["field"], "n_objects": len(f["objects"]),
                 "n_kept": f["n_kept"], "n_frame_contact": f["n_frame_contact"], "image_trimmed": f["image_trimmed"]}
                for f in self.doc["fields"]]

    def save_state(self, state: Any) -> None:
        """Validate and atomically write the page state."""
        if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
            raise ValueError(f"state must be a JSON object with schema {STATE_SCHEMA!r}")
        unknown = sorted(set(state.get("fields") or {}) - set(self.fields))
        if unknown:
            raise ValueError(f"unknown image id(s) in state: {unknown[:5]}")
        with self.lock:
            atomic_write_json(self.state_path, state)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        with self.lock:
            return json.loads(self.state_path.read_text(encoding="utf-8"))

    def enhance(self, req: Any) -> tuple[bytes, dict[str, Any]]:
        """Recompute one preset on one image with user parameters; returns (JPEG bytes, metrics)."""
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        image, preset, params = req.get("image"), req.get("preset"), req.get("params") or {}
        if image not in self.inventory.index:
            raise ValueError("unknown image id")
        if preset not in PRESET_NAMES:
            raise ValueError(f"preset must be one of {PRESET_NAMES}")
        u, ec = self.cfg.ui, self.cfg.enhance

        def clamp(key: str, rng: list[float]) -> float:
            try:
                v = float(params[key])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"parameter {key} is required for preset {preset}") from None
            return float(min(max(v, rng[0]), rng[1]))

        if preset == "figure-default":
            ec = dataclasses.replace(ec, figure_default=dataclasses.replace(
                ec.figure_default, median_target=clamp("median_target", u.median_target_range)))
        elif preset == "clahe":
            ec = dataclasses.replace(ec, clahe=dataclasses.replace(ec.clahe, clip_limit=clamp("clip_limit", u.clahe_clip_range)))
        elif preset == "tone-unsharp":
            ec = dataclasses.replace(ec, tone_unsharp=dataclasses.replace(
                ec.tone_unsharp, amount=clamp("amount", u.unsharp_amount_range)))
        out, _ = get_preset(preset, ec)(self._gray(image))
        met = preview_metrics(out, ec)
        size = fit_size(out.shape[1], out.shape[0], u.preview_width_px)
        return encode_jpeg(out, size, u.jpeg_quality), met

    def overlay(self, image: str) -> np.ndarray:
        """Full-resolution numbered overlay of a field (as in step 7) for the assistant."""
        from ..steps_image import enhanced_or_raw

        d6 = require_step(self.cfg, 6)
        fields = pd.read_csv(d6 / "fields.csv").set_index("image")
        lab = tifffile.imread(label_path(self.cfg, fields.loc[image, "label_tif"])).astype(np.int32)
        bg = enhanced_or_raw(self.cfg, image, self.inventory.loc[image, "path"], lab.shape)
        objs = self.fields[image]["objects"]
        kept = [o["label"] for o in objs if o["group_rule"] == GROUP_KEPT]
        frame = [o["label"] for o in objs if o["group_rule"] == GROUP_FRAME]
        return numbered_overlay(bg, lab, kept, frame, {o["label"]: o["area_px"] for o in objs}, self.cfg.review)

    # ------------------------------------------------------------- decisions
    def proposals(self) -> dict[str, Any]:
        """Decisions-tab document (proposals, decisions, thumbnails), rebuilt on every call."""
        from .dataset import collect_decisions

        return collect_decisions(self.cfg)

    def decide(self, req: Any) -> dict[str, Any]:
        """Write a decision (and its snapshot) from a review-page request."""
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        pid, choice = req.get("proposal_id"), req.get("choice")
        sha, comment = req.get("proposal_sha256"), req.get("comment") or ""
        if not isinstance(choice, str) or not isinstance(sha, str) or not isinstance(comment, str):
            raise ValueError("choice, proposal_sha256 and comment must be strings")
        d = records.decide(self.cfg, pid, choice, comment, sha, source="review-ui")
        return {k: d[k] for k in ("proposal_id", "choice", "decided_utc", "snapshot")}

    def snapshot(self, req: Any) -> dict[str, Any]:
        """Manual snapshot of a proposal card (optionally one option)."""
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        option, comment = req.get("option_id"), req.get("comment") or ""
        if (option is not None and not isinstance(option, str)) or not isinstance(comment, str):
            raise ValueError("option_id and comment must be strings")
        r = records.manual_snapshot(self.cfg, req.get("proposal_id"), option, comment)
        return {"snapshot": Path(r["snapshot_dir"]).name, "files": r["files"]}

    def suggest(self, image: str) -> dict[str, Any]:
        """Assistant suggestions for one field. Never changes the saved state."""
        if self.assistant is None:
            raise LLMError("the assistant is disabled (ui.llm.enabled: false)")
        if image not in self.fields:
            raise KeyError(image)
        out, cached = self.assistant.suggest(image, self.overlay(image), self.fields[image]["objects"])
        return {"field": image, "suggestions": out, "cached": cached}


def make_handler(app: UIApp) -> type[BaseHTTPRequestHandler]:
    """Request handler class bound to ``app``."""
    max_body = int(app.cfg.ui.max_request_bytes)
    origins = set(app.cfg.ui.cors_origins)

    class Handler(BaseHTTPRequestHandler):
        server_version = "morphoagent-ui"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # never log query strings (tokens)
            LOG.info("%s %s %s", self.command, urlparse(self.path).path, args[1] if len(args) > 1 else "")

        # -------------------------------------------------------- plumbing
        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            if origin and origin in origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-BFM-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

        def _error(self, status: int, msg: str) -> None:
            self._json(status, {"error": msg})

        def _authorised(self) -> bool:
            origin = self.headers.get("Origin")
            if origin and origin not in origins and not self._same_origin(origin):
                self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused (ui.cors_origins)")
                return False
            if app.token is None:
                return True
            given = self.headers.get("X-BFM-Token") or (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            if hmac.compare_digest(given.encode(), app.token.encode()):
                return True
            self._error(HTTPStatus.UNAUTHORIZED, "missing or wrong access token")
            return False

        def _same_origin(self, origin: str) -> bool:
            host = self.headers.get("Host", "")
            return urlparse(origin).netloc == host

        def _body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            if n > max_body:
                raise ValueError(f"request body larger than ui.max_request_bytes ({max_body})")
            return json.loads(self.rfile.read(n).decode("utf-8") or "null")

        # -------------------------------------------------------- verbs
        def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
            if self.headers.get("Origin") in origins:
                self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")
            else:
                self._error(HTTPStatus.FORBIDDEN, "CORS disabled")

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorised():
                return
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    self._send(HTTPStatus.OK, app.html, "text/html; charset=utf-8")
                elif path == "/api/fields":
                    self._json(HTTPStatus.OK, {"fields": app.field_list()})
                elif path == "/api/proposals":
                    self._json(HTTPStatus.OK, app.proposals())
                elif path == "/api/state":
                    self._json(HTTPStatus.OK, app.load_state())
                elif path.startswith("/api/suggest/"):
                    image = unquote(path[len("/api/suggest/"):])
                    if image not in app.fields:
                        self._error(HTTPStatus.NOT_FOUND, "unknown field")
                        return
                    self._json(HTTPStatus.OK, app.suggest(image))
                else:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
            except RateLimitError as exc:
                self._error(HTTPStatus.TOO_MANY_REQUESTS, str(exc))
            except LLMError as exc:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE if "disabled" in str(exc) else HTTPStatus.BAD_GATEWAY, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised():
                return
            path = urlparse(self.path).path
            try:
                body = self._body()
                if path == "/api/save":
                    app.save_state(body)
                    self._json(HTTPStatus.OK, {"saved": app.state_path.name})
                elif path == "/api/enhance":
                    jpeg, met = app.enhance(body)
                    self._send(HTTPStatus.OK, jpeg, "image/jpeg", {"X-BFM-Metrics": json.dumps(met)})
                elif path == "/api/decide":
                    self._json(HTTPStatus.OK, app.decide(body))
                elif path == "/api/snapshot":
                    self._json(HTTPStatus.OK, app.snapshot(body))
                else:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
            except records.UnknownProposalError as exc:
                self._error(HTTPStatus.NOT_FOUND, str(exc))
            except (records.DecisionExistsError, records.StaleProposalError) as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except OSError as exc:
                LOG.error("could not complete %s: %s", path, exc)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "server could not write the file; see the server log")

    return Handler


def make_server(cfg: Config, host: str | None = None, port: int | None = None,
                assistant: Assistant | None = None) -> tuple[ThreadingHTTPServer, UIApp]:
    """Create (but do not start) the HTTP server; enforces the token rule before anything is built."""
    host = host or cfg.ui.host
    port = cfg.ui.port if port is None else int(port)
    token = resolve_token(host, cfg.ui.token_env)
    httpd = ThreadingHTTPServer((host, port), BaseHTTPRequestHandler)  # bind first: fail fast if the port is taken
    try:
        app = UIApp(cfg, token, assistant)
    except BaseException:
        httpd.server_close()
        raise
    httpd.RequestHandlerClass = make_handler(app)
    httpd.daemon_threads = True
    return httpd, app


def run_ui_serve(cfg: Config, host: str | None = None, port: int | None = None) -> None:
    """Serve the review page until interrupted (Ctrl+C)."""
    httpd, app = make_server(cfg, host, port)
    h, p = httpd.server_address[:2]
    shown = f"[{h}]" if ":" in str(h) else h
    LOG.info("serving http://%s:%s/ (state file: %s)%s", shown, p, app.state_path,
             f"  token required: open the page with ?token=<value of ${cfg.ui.token_env}>" if app.token else "")
    if cfg.ui.llm.enabled:
        LOG.warning("LLM assistant ENABLED: field images and object tables are sent to %s on request",
                    app.doc["llm"]["endpoint_host"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopped")
    finally:
        httpd.server_close()
