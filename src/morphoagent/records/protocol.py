"""Proposal / decision protocol (schemas ``morphoagent-proposal/1`` and ``morphoagent-decision/1``).

Layout of the records folder (``agent.records_dir``, default
``<output_dir>/<agent.output_subdir>/records``)::

    proposals/<proposal_id>.json        written once by the agent (``propose``)
    proposals/<proposal_id>_files/      copies of the figures, SVGs and source tables it references
    decisions/<proposal_id>.json        written once when the researcher decides
    snapshots/<UTC time>_<stage>_<slug>/ view.png, view.svg, source.csv, state.json, note.json
    run_manifest.json                   counts and an event log, updated on every write

``proposal_id`` is ``<stage>_<YYYYMMDDTHHMMSSZ>_<6 hex digits>``. Proposal and
decision files are created with exclusive-create semantics and are never
replaced. See docs/protocol.md for the full specification.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .. import __version__
from ..pipeline.common import MANIFEST, file_sha256, package_versions
from ..pipeline.config import Config

LOG = logging.getLogger("morphoagent")

PROPOSAL_SCHEMA = "morphoagent-proposal/1"
DECISION_SCHEMA = "morphoagent-decision/1"
REJECTED = "rejected"

#: Stages in pipeline order. A stage tool refuses to run while a proposal of an
#: earlier stage is undecided (``agent.require_decisions``).
STAGES: tuple[str, ...] = ("consult", "contrast", "segment", "cut", "cluster", "literature")
STAGE_TITLES: dict[str, str] = {
    "consult": "Stage 0 - consultation",
    "contrast": "Stage 1 - contrast",
    "segment": "Stage 2 - segmentation",
    "cut": "Stage 2 - cut-offs",
    "cluster": "Stage 3 - shape modes",
    "literature": "Stage 4 - literature",
}
FIGURE_SUFFIXES = (".png", ".jpg", ".jpeg")
PROPOSAL_ID_RE = re.compile(r"^(?P<stage>[a-z]+)_(?P<time>\d{8}T\d{6}Z)_(?P<token>[0-9a-f]{6})$")
OPTION_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$"
REFERENCE_URLS = {"pmid": "https://pubmed.ncbi.nlm.nih.gov/", "doi": "https://doi.org/"}

_LOCK = threading.Lock()
_PMID = re.compile(r"^\s*pmid\s*[:\s]\s*(\d{1,9})\s*$", re.I)
_DOI = re.compile(r"^\s*(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)\s*$", re.I)

Scalar = str | int | float | bool | None


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class RecordsError(ValueError):
    """Invalid proposal, decision or snapshot request."""


class UnknownProposalError(RecordsError):
    """No proposal with this id exists in the records folder."""


class DecisionExistsError(RecordsError):
    """The proposal has already been decided; decisions are never replaced."""


class StaleProposalError(RecordsError):
    """The proposal file changed after the review page loaded it (hash mismatch)."""


class DecisionRequiredError(RuntimeError):
    """A stage tool was called while a proposal of an earlier stage is undecided."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def normalize_reference(ref: str) -> str:
    """Return ``PMID:<digits>`` or ``doi:<10.x/...>``; raise ``ValueError`` for anything else.

    Accepted spellings: ``PMID 123``, ``pmid:123``, ``doi:10.1000/xyz``,
    ``10.1000/xyz`` and ``https://doi.org/10.1000/xyz``. Only the format is
    checked; whether the identifier exists is not.
    """
    m = _PMID.match(ref)
    if m:
        return f"PMID:{int(m.group(1))}"
    m = _DOI.match(ref)
    if m:
        return f"doi:{m.group(1)}"
    raise ValueError(f"reference {ref!r} is neither a PMID ('PMID:<digits>') nor a DOI ('doi:10.<prefix>/<suffix>')")


def _refs(v: list[str]) -> list[str]:
    return list(dict.fromkeys(normalize_reference(r) for r in v))


class OptionIn(BaseModel):
    """One candidate the researcher can choose (as written by the agent)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=OPTION_ID_RE, description="Short option id, unique within the proposal (not 'rejected')")
    label: str = Field(min_length=1, max_length=200, description="One-line label shown on the card")
    description: str = Field("", max_length=2000, description="Optional longer text")
    figures: list[str] = Field(default_factory=list, description=(
        "PNG/JPEG figure files. '<stem>.svg' and 'source_<stem>.csv' next to a figure are copied with it"))
    table: list[dict[str, Scalar]] = Field(default_factory=list, max_length=500,
                                           description="Rows (column -> value) shown under the figures")
    numbers: dict[str, Scalar] = Field(default_factory=dict, description="Key numbers of this option (name -> value)")
    references: list[str] = Field(default_factory=list, description="'PMID:<digits>' or 'doi:10.x/...' strings")
    config_changes: dict[str, Any] = Field(default_factory=dict, description=(
        "Dotted configuration keys and the values to write when this option is chosen, e.g. "
        "{'select.area_min_px': 1500}"))

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v.lower() == REJECTED:
            raise ValueError("'rejected' is reserved for decisions")
        return v

    @field_validator("references")
    @classmethod
    def _norm_refs(cls, v: list[str]) -> list[str]:
        return _refs(v)


class Option(OptionIn):
    """An option as stored: ``figures`` are paths relative to the ``proposals`` folder."""

    figure_origins: list[str] = Field(default_factory=list, description="Paths the figures were copied from")


class Evidence(BaseModel):
    """One piece of evidence behind the recommendation (a plain string is accepted as ``text``)."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    path: str | None = Field(None, description="File the statement was read from")
    tool: str | None = Field(None, description="Tool whose output the statement quotes")
    references: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, v: Any) -> Any:
        return {"text": v} if isinstance(v, str) else v

    @field_validator("references")
    @classmethod
    def _norm_refs(cls, v: list[str]) -> list[str]:
        return _refs(v)


class Provenance(BaseModel):
    """Where the candidates came from (tool, parameters, result folder)."""

    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    run_dir: str | None = None
    manifest: str | None = None


class Proposal(BaseModel):
    """Schema ``morphoagent-proposal/1``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["morphoagent-proposal/1"] = Field(PROPOSAL_SCHEMA, alias="schema")
    proposal_id: str = Field(pattern=PROPOSAL_ID_RE.pattern)
    stage: Literal["consult", "contrast", "segment", "cut", "cluster", "literature"]
    title: str = Field(min_length=1, max_length=200)
    created_utc: str
    rationale: str = Field("", max_length=5000)
    recommended: str | None = None
    options: list[Option] = Field(min_length=1, max_length=50)
    evidence: list[Evidence] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)
    main_figure: str | None = Field(None, description="Relative path of the figure used for snapshots")
    figure_state: dict[str, Any] = Field(default_factory=dict, description="Figure settings copied to state.json")
    morphoagent_version: str = __version__

    @model_validator(mode="after")
    def _consistent(self) -> Proposal:
        ids = [o.id for o in self.options]
        if len(set(ids)) != len(ids):
            raise ValueError(f"option ids must be unique, got {ids}")
        if self.recommended is not None and self.recommended not in ids:
            raise ValueError(f"recommended {self.recommended!r} is not an option id {ids}")
        if not self.proposal_id.startswith(self.stage + "_"):
            raise ValueError("proposal_id must start with '<stage>_'")
        return self

    def option(self, option_id: str) -> Option:
        """Option with this id (``KeyError`` when absent)."""
        for o in self.options:
            if o.id == option_id:
                return o
        raise KeyError(option_id)

    def dump(self) -> dict[str, Any]:
        """JSON-ready dict with the ``schema`` key."""
        return self.model_dump(mode="json", by_alias=True)


class Decision(BaseModel):
    """Schema ``morphoagent-decision/1``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["morphoagent-decision/1"] = Field(DECISION_SCHEMA, alias="schema")
    proposal_id: str = Field(pattern=PROPOSAL_ID_RE.pattern)
    stage: Literal["consult", "contrast", "segment", "cut", "cluster", "literature"]
    choice: str = Field(description="Chosen option id, or 'rejected'")
    comment: str = Field("", max_length=5000)
    decided_utc: str
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["review-ui", "review-ui-download", "api"] = "api"
    snapshot: str | None = Field(None, description="Snapshot folder relative to the records folder")
    morphoagent_version: str = __version__

    def dump(self) -> dict[str, Any]:
        """JSON-ready dict with the ``schema`` key."""
        return self.model_dump(mode="json", by_alias=True)


# --------------------------------------------------------------------------- #
# Paths and file helpers
# --------------------------------------------------------------------------- #
def records_dir(cfg: Config) -> Path:
    """``agent.records_dir`` or ``<output_dir>/<agent.output_subdir>/records``."""
    if cfg.agent.records_dir:
        return Path(cfg.agent.records_dir)
    return Path(cfg.output_dir) / cfg.agent.output_subdir / "records"


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def iso(t: _dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def compact(t: _dt.datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%SZ")


def slugify(text: str, max_len: int = 40) -> str:
    """Lower-case ASCII slug (letters, digits, single hyphens)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-") or "untitled")


def write_new_json(path: Path, obj: Any) -> None:
    """Create ``path`` exclusively (``FileExistsError`` if it exists) and write JSON to it."""
    data = json.dumps(obj, indent=1, ensure_ascii=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
    except FileExistsError:
        raise
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _check_id(proposal_id: str) -> str:
    if not isinstance(proposal_id, str) or not PROPOSAL_ID_RE.match(proposal_id):
        raise UnknownProposalError(f"not a proposal id: {proposal_id!r}")
    return proposal_id


def proposal_path(cfg: Config, proposal_id: str) -> Path:
    return records_dir(cfg) / "proposals" / f"{_check_id(proposal_id)}.json"


def decision_path(cfg: Config, proposal_id: str) -> Path:
    return records_dir(cfg) / "decisions" / f"{_check_id(proposal_id)}.json"


def sibling_svg(fig: Path) -> Path:
    """``<stem>.svg`` next to a figure."""
    return fig.with_suffix(".svg")


def sibling_source(fig: Path) -> Path:
    """``source_<stem>.csv`` next to a figure (the convention of every pipeline figure)."""
    return fig.with_name(f"source_{fig.stem}.csv")


def _find_manifest(fig: Path) -> Path | None:
    for d in (fig.parent, fig.parent.parent):
        if (d / MANIFEST).is_file():
            return d / MANIFEST
    return None


def _event(cfg: Config, action: str, **info: Any) -> None:
    """Update ``records/run_manifest.json`` (counts and an append-only event list)."""
    from ..pipeline.ui.llm import atomic_write_json

    root = records_dir(cfg)
    p = root / MANIFEST
    try:
        m = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        m = {}
    props = sorted((root / "proposals").glob("*.json"))
    decs = sorted((root / "decisions").glob("*.json"))
    snaps = [d for d in (root / "snapshots").glob("*") if d.is_dir()] if (root / "snapshots").is_dir() else []
    m.update({
        "tool": "records",
        "timestamp_utc": iso(utc_now()),
        "versions": package_versions(),
        "config_file": cfg.config_path,
        "config_sha256": file_sha256(cfg.config_path),
        "parameters": {"stages": list(STAGES), "require_decisions": cfg.agent.require_decisions,
                       "proposal_schema": PROPOSAL_SCHEMA, "decision_schema": DECISION_SCHEMA},
        "counts_out": {"proposals": len(props), "decisions": len(decs), "pending": len(props) - len(decs),
                       "snapshots": len(snaps)},
    })
    m.setdefault("events", []).append({"time_utc": iso(utc_now()), "action": action, **info})
    atomic_write_json(p, m)


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #
def _as_option(o: Any) -> OptionIn:
    if isinstance(o, OptionIn):
        return OptionIn.model_validate(o.model_dump(include=set(OptionIn.model_fields)))
    return OptionIn.model_validate(o)


def propose(
    cfg: Config,
    stage: str,
    title: str,
    options: list[Any],
    recommended: str | None = None,
    rationale: str = "",
    evidence: list[Any] | None = None,
    provenance: Any = None,
    main_figure: str | None = None,
    figure_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a proposal for the researcher to decide on the review page.

    Every figure file is copied (with its ``<stem>.svg`` and
    ``source_<stem>.csv`` siblings when present) into
    ``proposals/<proposal_id>_files/``, so later re-runs with ``overwrite``
    cannot change what the researcher sees.

    Parameters
    ----------
    cfg : Config
        Loaded configuration (locates the records folder).
    stage : str
        One of :data:`STAGES`.
    title : str
        Card title.
    options : list
        :class:`OptionIn` objects or dicts with the same keys.
    recommended : str, optional
        Option id the agent recommends.
    rationale : str
        Why the recommendation (shown on the card and in the report).
    evidence : list, optional
        :class:`Evidence` objects, dicts or plain strings.
    provenance : Provenance or dict, optional
        Tool and parameters behind the candidates. When omitted, the
        ``run_manifest.json`` next to (or one level above) the first figure is
        read for ``tool``/``name`` and ``parameters``.
    main_figure : str, optional
        One of the figure paths given in ``options``; used for snapshots that
        are not tied to an option. Default: the first figure of the
        recommended option, else the first figure of any option.
    figure_state : dict, optional
        Figure settings stored with snapshots (``state.json``).

    Returns
    -------
    dict
        ``proposal_id``, ``path``, ``sha256``, ``stage``, ``n_options``,
        ``files`` (copied files, relative to ``proposals/``) and ``next``
        (how the researcher decides).
    """
    if stage not in STAGES:
        raise RecordsError(f"stage must be one of {list(STAGES)}, got {stage!r}")
    opts = [_as_option(o) for o in options]
    if not opts:
        raise RecordsError("a proposal needs at least one option")
    srcs: list[list[Path]] = []
    for o in opts:
        paths = []
        for f in o.figures:
            p = Path(f).expanduser()
            if p.suffix.lower() not in FIGURE_SUFFIXES:
                raise RecordsError(f"option {o.id}: figure {f} must be one of {FIGURE_SUFFIXES} "
                                   "(an SVG next to it is picked up automatically)")
            if not p.is_file():
                raise RecordsError(f"option {o.id}: figure file not found: {f}")
            paths.append(p.resolve())
        srcs.append(paths)
    all_figs = [(o.id, p) for o, ps in zip(opts, srcs, strict=True) for p in ps]
    main_src: Path | None = None
    if main_figure:
        mp = Path(main_figure).expanduser().resolve()
        if mp not in {p for _, p in all_figs}:
            raise RecordsError("main_figure must be one of the figure paths given in options")
        main_src = mp
    root = records_dir(cfg) / "proposals"
    root.mkdir(parents=True, exist_ok=True)
    t = utc_now()
    for _ in range(20):
        pid = f"{stage}_{compact(t)}_{secrets.token_hex(3)}"
        files_dir = root / f"{pid}_files"
        try:
            files_dir.mkdir()
            break
        except FileExistsError:
            continue
    else:  # pragma: no cover - 20 collisions of a 24-bit token in one second
        raise RecordsError("could not allocate a proposal id")
    copied: list[str] = []
    stored: list[Option] = []
    main_rel: str | None = None
    try:
        for o, paths in zip(opts, srcs, strict=True):
            rels = []
            for p in paths:
                dest = files_dir / f"{o.id}__{p.stem}{p.suffix.lower()}"
                n = 2
                while dest.exists():
                    dest = files_dir / f"{o.id}__{p.stem}-{n}{p.suffix.lower()}"
                    n += 1
                shutil.copy2(p, dest)
                rel = f"{files_dir.name}/{dest.name}"
                rels.append(rel)
                copied.append(rel)
                for side, target in ((sibling_svg(p), sibling_svg(dest)), (sibling_source(p), sibling_source(dest))):
                    if side.is_file():
                        shutil.copy2(side, target)
                        copied.append(f"{files_dir.name}/{target.name}")
                if main_src is not None and p == main_src and main_rel is None:
                    main_rel = rel
            stored.append(Option(**{**o.model_dump(), "figures": rels}, figure_origins=[str(p) for p in paths]))
        if main_rel is None:
            first = None
            if recommended is not None:
                first = next((s.figures[0] for s in stored if s.id == recommended and s.figures), None)
            first = first or next((s.figures[0] for s in stored if s.figures), None)
            main_rel = first
        prov = Provenance.model_validate(provenance) if provenance is not None else Provenance()
        if prov.tool is None and all_figs:
            mf = _find_manifest(all_figs[0][1])
            if mf is not None:
                try:
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    prov = Provenance(tool=str(m.get("tool") or m.get("name") or "") or None,
                                      parameters=m.get("parameters") or {}, run_dir=str(mf.parent), manifest=str(mf))
                except (OSError, ValueError) as exc:
                    LOG.warning("could not read %s: %s", mf, exc)
        prop = Proposal(proposal_id=pid, stage=stage, title=title, created_utc=iso(t), rationale=rationale,
                        recommended=recommended, options=stored, evidence=list(evidence or []), provenance=prov,
                        main_figure=main_rel, figure_state=dict(figure_state or {}))
        path = root / f"{pid}.json"
        write_new_json(path, prop.dump())
    except BaseException:
        shutil.rmtree(files_dir, ignore_errors=True)
        raise
    sha = file_sha256(path)
    _event(cfg, "propose", proposal_id=pid, stage=stage, n_options=len(stored))
    LOG.info("proposal %s written (%d option(s)); waiting for a decision on the review page", pid, len(stored))
    return {
        "proposal_id": pid, "stage": stage, "path": str(path), "sha256": sha, "n_options": len(stored),
        "recommended": recommended, "files": copied,
        "next": "The researcher decides on the Decisions tab of `morphoagent ui serve -c <config>`; "
                "poll get_decision(proposal_id) and do not run later stages before it is decided.",
    }


def load_proposal(cfg: Config, proposal_id: str) -> tuple[Proposal, str]:
    """Proposal and the SHA-256 of its file; :class:`UnknownProposalError` when absent."""
    p = proposal_path(cfg, proposal_id)
    if not p.is_file():
        raise UnknownProposalError(f"unknown proposal {proposal_id}")
    raw = p.read_bytes()
    return Proposal.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def get_decision(cfg: Config, proposal_id: str) -> dict[str, Any] | None:
    """The decision of a proposal, or ``None`` while it is undecided."""
    load_proposal(cfg, proposal_id)
    p = decision_path(cfg, proposal_id)
    if not p.is_file():
        return None
    return Decision.model_validate_json(p.read_bytes()).dump()


def list_proposals(cfg: Config) -> list[dict[str, Any]]:
    """Every proposal (oldest first: creation time, then file time) with its hash and decision (``None`` when pending).

    Files that do not validate are skipped with a warning.
    """
    root = records_dir(cfg) / "proposals"
    out = []
    for p in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            prop, sha = load_proposal(cfg, p.stem)
        except (RecordsError, ValueError, OSError) as exc:
            LOG.warning("skipping invalid proposal file %s: %s", p.name, exc)
            continue
        d = decision_path(cfg, prop.proposal_id)
        dec = None
        if d.is_file():
            try:
                dec = Decision.model_validate_json(d.read_bytes()).dump()
            except ValueError as exc:
                LOG.warning("invalid decision file %s: %s", d.name, exc)
        out.append({"proposal": prop.dump(), "sha256": sha, "decision": dec, "_order": p.stat().st_mtime_ns})
    out.sort(key=lambda r: (r["proposal"]["created_utc"], r.pop("_order"), r["proposal"]["proposal_id"]))
    return out


def list_pending(cfg: Config) -> list[dict[str, Any]]:
    """Undecided proposals as short summaries (id, stage, title, created, recommended, options)."""
    out = []
    for r in list_proposals(cfg):
        if r["decision"] is not None:
            continue
        p = r["proposal"]
        out.append({"proposal_id": p["proposal_id"], "stage": p["stage"], "title": p["title"],
                    "created_utc": p["created_utc"], "recommended": p["recommended"],
                    "options": [{"id": o["id"], "label": o["label"]} for o in p["options"]]})
    return out


def require_decision(cfg: Config, stage: str) -> None:
    """Raise :class:`DecisionRequiredError` while a proposal of an earlier stage is undecided.

    Does nothing when ``agent.require_decisions`` is false. Proposals of the
    same or a later stage do not block.
    """
    if stage not in STAGES:
        raise RecordsError(f"unknown stage {stage!r}")
    if not cfg.agent.require_decisions:
        return
    idx = STAGES.index(stage)
    blocking = [p for p in list_pending(cfg) if STAGES.index(p["stage"]) < idx]
    if blocking:
        ids = ", ".join(f"{p['proposal_id']} ({p['title']})" for p in blocking)
        raise DecisionRequiredError(
            f"stage '{stage}' is blocked: waiting for the researcher to decide {ids}. Ask them to open the "
            "Decisions tab of `morphoagent ui serve -c <config>`; check with get_decision(). "
            "(agent.require_decisions: true)")


# --------------------------------------------------------------------------- #
# Decisions and snapshots
# --------------------------------------------------------------------------- #
def decide(
    cfg: Config,
    proposal_id: str,
    choice: str,
    comment: str = "",
    proposal_sha256: str | None = None,
    source: str = "api",
    decided_utc: str | None = None,
) -> dict[str, Any]:
    """Record the researcher's decision and write its snapshot.

    Parameters
    ----------
    cfg : Config
        Loaded configuration.
    proposal_id : str
        Proposal being decided.
    choice : str
        An option id or ``"rejected"``.
    comment : str
        Free text.
    proposal_sha256 : str, optional
        Hash of the proposal file the researcher saw; a mismatch raises
        :class:`StaleProposalError`.
    source : {"review-ui", "review-ui-download", "api"}
        Where the decision came from.
    decided_utc : str, optional
        Time of the decision (default: now).

    Returns
    -------
    dict
        The decision document plus ``path`` and ``snapshot_dir``.
    """
    from .snapshots import write_snapshot

    with _LOCK:
        prop, sha = load_proposal(cfg, proposal_id)
        if proposal_sha256 is not None and proposal_sha256 != sha:
            raise StaleProposalError(f"proposal {proposal_id} changed since it was loaded (hash mismatch); reload the page")
        ids = [o.id for o in prop.options]
        if choice != REJECTED and choice not in ids:
            raise RecordsError(f"choice must be one of {ids} or 'rejected', got {choice!r}")
        dpath = decision_path(cfg, proposal_id)
        if dpath.exists():
            raise DecisionExistsError(f"proposal {proposal_id} has already been decided ({dpath.name}); decisions are final")
        when = decided_utc or iso(utc_now())
        snap = write_snapshot(cfg, prop, sha, kind="decision", option_id=None if choice == REJECTED else choice,
                              choice=choice, comment=comment, time_utc=when,
                              decision_file=f"decisions/{proposal_id}.json")
        dec = Decision(proposal_id=proposal_id, stage=prop.stage, choice=choice, comment=comment, decided_utc=when,
                       proposal_sha256=sha, source=source, snapshot=snap.relative_to(records_dir(cfg)).as_posix())
        try:
            write_new_json(dpath, dec.dump())
        except FileExistsError:
            shutil.rmtree(snap, ignore_errors=True)
            raise DecisionExistsError(f"proposal {proposal_id} has already been decided") from None
        _event(cfg, "decide", proposal_id=proposal_id, choice=choice, snapshot=dec.snapshot)
    LOG.info("decision for %s: %s (snapshot %s)", proposal_id, choice, snap.name)
    return {**dec.dump(), "path": str(dpath), "snapshot_dir": str(snap)}


def import_decision(cfg: Config, file: str | Path) -> dict[str, Any]:
    """Record a decision file downloaded from the offline review page (validated, hash-checked)."""
    try:
        doc = Decision.model_validate_json(Path(file).read_bytes())
    except ValueError as exc:
        raise RecordsError(f"{file}: not a valid {DECISION_SCHEMA} document: {exc}") from exc
    prop, _ = load_proposal(cfg, doc.proposal_id)
    if prop.stage != doc.stage:
        raise RecordsError(f"{file}: stage {doc.stage!r} does not match the proposal ({prop.stage!r})")
    return decide(cfg, doc.proposal_id, doc.choice, doc.comment, doc.proposal_sha256, source="review-ui-download",
                  decided_utc=doc.decided_utc)


def manual_snapshot(cfg: Config, proposal_id: str, option_id: str | None = None, comment: str = "") -> dict[str, Any]:
    """Snapshot of a proposal (optionally one option's figure) without deciding it."""
    from .snapshots import write_snapshot

    with _LOCK:
        prop, sha = load_proposal(cfg, proposal_id)
        if option_id is not None and option_id not in {o.id for o in prop.options}:
            raise RecordsError(f"unknown option {option_id!r} of {proposal_id}")
        dec = get_decision(cfg, proposal_id)
        snap = write_snapshot(cfg, prop, sha, kind="manual", option_id=option_id,
                              choice=dec["choice"] if dec else None, comment=comment, time_utc=iso(utc_now()),
                              decision_file=f"decisions/{proposal_id}.json" if dec else None)
        _event(cfg, "snapshot", proposal_id=proposal_id, option_id=option_id,
               snapshot=snap.relative_to(records_dir(cfg)).as_posix())
    return {"snapshot_dir": str(snap), "files": sorted(p.name for p in snap.iterdir())}
