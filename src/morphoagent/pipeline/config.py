"""Typed configuration.

Every default value lives twice, on purpose: in the dataclasses below and in
``configs/default.yaml``. A unit test asserts that both are identical, so the
shipped YAML is always a complete, accurate reference of every key.

A user configuration only needs the keys it changes; everything else falls back
to the defaults. Unknown keys are rejected so that a typo never silently turns
into a default.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_type_hints

import yaml

PRESET_NAMES = ("figure-default", "tone-unsharp", "clahe", "none")


class ConfigError(ValueError):
    """Raised for an invalid configuration file or value."""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
@dataclass
class InputConfig:
    """Where the images are and how their file names are parsed."""

    image_dir: str = "/path/to/images"
    extensions: list[str] = field(default_factory=lambda: [".jpg", ".jpeg", ".png", ".tif", ".tiff"])
    strip_prefix: str = ""
    strip_suffix: str = ""
    name_regex: str = r"^(?P<condition>.+)_(?P<field>Image\d+)$"
    skip_max_at_or_below: int = 0


@dataclass
class ConditionsConfig:
    """Condition order, reference condition and plot colours."""

    order: list[str] = field(default_factory=list)
    reference: str | None = None
    colors: dict[str, str] = field(default_factory=dict)
    default_color: str = "#FFFFFF"


@dataclass
class FigureDefaultParams:
    """Parameters of the ``figure-default`` enhancement preset."""

    output_size: list[int] | None = field(default_factory=lambda: [960, 720])
    median_target: float = 125.0
    kernel_path: str | None = None
    tone_lut_path: str | None = None


@dataclass
class ToneUnsharpParams:
    """Parameters of the ``tone-unsharp`` enhancement preset."""

    source_percentiles: list[float] = field(default_factory=lambda: [1.0, 50.0, 95.0, 99.0])
    target_values: list[float] = field(default_factory=lambda: [62.0, 179.0, 235.0, 255.0])
    sigma_px: float = 4.0
    amount: float | None = None
    amount_grid: list[float] = field(default_factory=lambda: [0.0, 8.0, 0.1])
    display_height_px: int = 503
    reference_edge_metric: float = 3.5468
    target_fraction: float = 1.2


@dataclass
class ClaheParams:
    """Parameters of the ``clahe`` preset (skimage ``equalize_adapthist``)."""

    clip_limit: float = 0.01
    kernel_size: int | None = None
    nbins: int = 256


@dataclass
class EnhanceConfig:
    """Steps 2 and 3: contrast / brightness enhancement."""

    preset: str = "figure-default"
    samples: list[str] | None = None
    max_sample_conditions: int = 4
    preview_presets: list[str] = field(default_factory=lambda: list(PRESET_NAMES))
    metrics_width_px: int = 448
    sheet_panel_width_px: int = 480
    edge_top_fraction: float = 0.02
    noise_sigma_px: float = 1.5
    flat_gradient_sigma_px: float = 3.0
    flat_fraction: float = 0.30
    figure_default: FigureDefaultParams = field(default_factory=FigureDefaultParams)
    tone_unsharp: ToneUnsharpParams = field(default_factory=ToneUnsharpParams)
    clahe: ClaheParams = field(default_factory=ClaheParams)


@dataclass
class TuneConfig:
    """Step 4: Cellpose-SAM parameter grid shown as outline montages."""

    samples: list[str] | None = None
    max_sample_conditions: int = 4
    flow_threshold: list[float] = field(default_factory=lambda: [0.8])
    cellprob_threshold: list[float] = field(default_factory=lambda: [-1.0, -2.0, -3.0, -4.0])
    diameter: list[float | None] = field(default_factory=lambda: [None, 60.0, 100.0])
    downscale: list[float] = field(default_factory=lambda: [0.125, 0.25])
    panel_width_px: int = 480
    outline_color: list[int] = field(default_factory=lambda: [255, 40, 40])


@dataclass
class SegmentConfig:
    """Step 5: Cellpose-SAM segmentation."""

    model: str = "cpsam"
    gpu: bool = True
    flow_threshold: float = 0.8
    cellprob_threshold: float = -1.0
    diameter: float | None = None
    downscale: float = 0.125
    label_compression: str = "zlib"
    tune: TuneConfig = field(default_factory=TuneConfig)


@dataclass
class MeasureConfig:
    """Where shape features are measured (steps 5, 6 and the ``cut_distributions`` tool).

    ``shape_grid`` is ``full`` (every feature on the full-resolution labels) or
    ``segmentation``: the axis features (major / minor axis length, aspect
    ratio, eccentricity) are measured on the labels sampled on the
    segmentation grid (``segment.downscale``), as Cellpose produced them.
    Objects changed after segmentation (largest piece, curation, off-grid
    hand edits) are always measured at full resolution.
    """

    shape_grid: str = "full"


@dataclass
class SelectConfig:
    """Step 6: rule-based object selection."""

    largest_piece: bool = True
    connectivity: int = 8
    area_min_px: float = 2000.0
    frame_contact_px: float = 24.0
    image_trim_fraction: float = 0.0
    image_trim_min_images: int = 2
    image_trim_features: list[str] = field(
        default_factory=lambda: ["form_factor", "solidity", "eccentricity", "aspect_ratio", "extent"]
    )
    image_trim_log_area: bool = True
    field_table: str | None = None


@dataclass
class ReviewConfig:
    """Step 7: numbered mask-review overlays and slide deck."""

    background: str = "enhanced"
    kept_color: list[int] = field(default_factory=lambda: [255, 40, 40])
    kept_linewidth_px: int = 6
    frame_contact_color: list[int] = field(default_factory=lambda: [255, 150, 150])
    frame_contact_linewidth_px: int = 3
    dash_px: int = 22
    font_path: str | None = None
    font_scale: float = 0.22
    font_min_px: int = 38
    font_max_px: int = 78
    text_color: list[int] = field(default_factory=lambda: [255, 255, 255])
    stroke_color: list[int] = field(default_factory=lambda: [0, 0, 0])
    stroke_min_px: int = 3
    stroke_divisor: int = 12
    slide_image_width_px: int = 1600
    jpeg_quality: int = 90


@dataclass
class CurateConfig:
    """Step 8: optional manual curation (delete / merge)."""

    edits_csv: str | None = None
    connectivity: int = 8


@dataclass
class ElbowConfig:
    """Range of K for the elbow (inertia versus K) analysis."""

    k_min: int = 2
    k_max: int = 20


@dataclass
class KMeansConfig:
    """K-means settings for the elbow; defaults equal those hard-coded in VAMPIRE."""

    init: str = "k-means++"
    n_init: int = 3
    max_iter: int = 300
    random_state: int = 9


@dataclass
class VampireConfig:
    """Step 9: VAMPIRE shape-mode clustering (external checkout)."""

    src_path: str = "/path/to/VAMPIRE_open/Source Code"
    python: str = "python"
    n_coordinates: int = 50
    k: list[int] = field(default_factory=lambda: [4])
    renumber_by: str = "aspect_ratio"
    elbow: ElbowConfig = field(default_factory=ElbowConfig)
    elbow_kmeans: KMeansConfig = field(default_factory=KMeansConfig)
    mode_colors: list[str] = field(
        default_factory=lambda: [
            "#98D8FE", "#6DB0E0", "#FB9AF4", "#F97374", "#B7E4A7",
            "#F9D976", "#C9B8E8", "#FACAA0", "#9AD0C2", "#D9D9D9",
        ]
    )
    contour_edge_color: str = "#3A4650"


@dataclass
class BoxPlotConfig:
    """Box + jitter plot geometry and style."""

    box_width: float = 0.6
    whisker_iqr: float = 1.5
    line_width_pt: float = 0.25
    median_line_width_pt: float = 1.4
    mean_marker_size: float = 5.0
    dot_size: float = 6.0
    dot_alpha: float = 0.45
    jitter_halfwidth: float = 0.2
    jitter_seed: int = 20261105
    axis_height_in: float = 3.0
    width_per_box_in: float = 0.55
    y_top: str = "whisker"


@dataclass
class ShannonConfig:
    """Optional Shannon diversity of shape modes with a bootstrap CI."""

    enabled: bool = False
    n_boot: int = 1000
    seed: int = 20261105
    ci: float = 95.0


@dataclass
class PixelCalibrationConfig:
    """Pixel size derived from the imaged field width (alternative to ``stats.pixel_size_um``).

    ``pixel size (um/px) = field_width_um / image_width_px / binning``, computed
    in this order in double precision, so the value is exact rather than a
    rounded number copied into the configuration. ``image_width_px`` is the
    sensor (or full-resolution) width the field width refers to; ``binning`` is
    the divisor between that width and the analysed images (e.g. 4 when the
    images were saved at a quarter of the sensor width).
    """

    field_width_um: float | None = None
    image_width_px: float | None = None
    binning: float = 1.0


@dataclass
class StatsConfig:
    """Step 10: object-level statistics and plots."""

    metrics: list[str] = field(
        default_factory=lambda: [
            "area_px", "aspect_ratio", "form_factor", "solidity", "eccentricity", "extent", "perimeter_px",
        ]
    )
    pixel_size_um: float | None = None
    pixel_calibration: PixelCalibrationConfig = field(default_factory=PixelCalibrationConfig)
    significance_from: str = "p"
    star_thresholds: list[float] = field(default_factory=lambda: [0.05, 0.01, 0.001])
    box: BoxPlotConfig = field(default_factory=BoxPlotConfig)
    shannon: ShannonConfig = field(default_factory=ShannonConfig)


@dataclass
class PlotConfig:
    """Global figure settings."""

    dpi: int = 300
    font_family: str = "Arial"
    font_paths: list[str] = field(default_factory=list)
    font_size_pt: float = 8.0


@dataclass
class LLMConfig:
    """Optional language-model assistant of ``morphoagent ui serve`` (disabled by default).

    When enabled, the numbered overlay image of ONE field and a compact object
    table are sent to ``endpoint`` on an explicit user request. See docs/ui.md
    for the request / response schema and the privacy notice.
    """

    enabled: bool = False
    provider: str = "generic-json"
    endpoint: str | None = None
    model: str | None = None
    api_key_env: str = "MORPHOAGENT_LLM_API_KEY"
    timeout_s: float = 60.0
    max_requests_per_minute: int = 6
    image_width_px: int = 1024
    jpeg_quality: int = 85
    max_objects: int = 400
    cache_file: str | None = None


@dataclass
class UIConfig:
    """Browser helper tools: ``morphoagent ui build`` (offline HTML) and ``morphoagent ui serve``."""

    max_width: int = 1280
    jpeg_quality: int = 85
    preview_width_px: int = 960
    median_target_grid: list[float] = field(default_factory=lambda: [105.0, 115.0, 125.0, 135.0, 145.0])
    html_path: str | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    token_env: str = "MORPHOAGENT_UI_TOKEN"
    cors_origins: list[str] = field(default_factory=list)
    state_file: str | None = None
    max_request_bytes: int = 20_000_000
    median_target_range: list[float] = field(default_factory=lambda: [60.0, 200.0, 1.0])
    clahe_clip_range: list[float] = field(default_factory=lambda: [0.001, 0.05, 0.001])
    unsharp_amount_range: list[float] = field(default_factory=lambda: [0.0, 8.0, 0.1])
    card_table_max_rows: int = 8
    card_table_max_columns: int = 6
    llm: LLMConfig = field(default_factory=LLMConfig)


@dataclass
class CutConfig:
    """Candidate cut-offs scanned by the ``cut_distributions`` agent tool.

    Candidates are multiples of the current ``select.area_min_px`` and
    ``select.frame_contact_px``, so they follow the configured values.
    """

    area_min_factors: list[float] = field(default_factory=lambda: [0.25, 0.5, 1.0, 1.5, 2.0])
    frame_contact_factors: list[float] = field(default_factory=lambda: [0.0, 0.5, 1.0, 2.0, 4.0])
    histogram_bins: int = 60


@dataclass
class ReportConfig:
    """``morphoagent report``: slide deck and Markdown report built from snapshots (docs/protocol.md)."""

    prefix: str = "morphoagent"
    max_table_rows: int = 12
    max_number_columns: int = 12
    max_table_columns: int = 6
    font_family: str = "Arial"
    redact_paths: bool = True


@dataclass
class AgentConfig:
    """Tools served to agents by ``morphoagent-mcp`` and the proposal / decision records (docs/mcp.md, docs/protocol.md)."""

    output_subdir: str = "agent"
    records_dir: str | None = None
    require_decisions: bool = True
    thumbnail_width_px: int = 360
    cut: CutConfig = field(default_factory=CutConfig)
    report: ReportConfig = field(default_factory=ReportConfig)


@dataclass
class LiteratureConfig:
    """Literature tools (``morphoagent-literature``, docs/literature.md): PubMed E-utilities and Europe PMC.

    No identity is sent unless configured: ``ncbi_email`` (or the variable named
    by ``ncbi_email_env``) and the API key in the variable named by
    ``ncbi_api_key_env`` are passed to NCBI only when set.
    """

    tool_name: str = "morphoagent"
    ncbi_email: str = ""
    ncbi_email_env: str = "MORPHOAGENT_NCBI_EMAIL"
    ncbi_api_key_env: str = "MORPHOAGENT_NCBI_API_KEY"
    eutils_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    europepmc_url: str = "https://www.ebi.ac.uk/europepmc/webservices/rest"
    ncbi_requests_per_second: float | None = None
    europepmc_requests_per_second: float = 5.0
    timeout_s: float = 20.0
    max_retries: int = 3
    backoff_s: float = 1.0
    cache_dir: str | None = None
    cache_ttl_days: float = 30.0
    offline: bool = False
    max_results: int = 50
    title_match_threshold: float = 0.9
    methods_max_paragraphs: int = 200


@dataclass
class Config:
    """Complete pipeline configuration."""

    output_dir: str = "/path/to/output"
    log_level: str = "INFO"
    input: InputConfig = field(default_factory=InputConfig)
    conditions: ConditionsConfig = field(default_factory=ConditionsConfig)
    enhance: EnhanceConfig = field(default_factory=EnhanceConfig)
    segment: SegmentConfig = field(default_factory=SegmentConfig)
    measure: MeasureConfig = field(default_factory=MeasureConfig)
    select: SelectConfig = field(default_factory=SelectConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    curate: CurateConfig = field(default_factory=CurateConfig)
    vampire: VampireConfig = field(default_factory=VampireConfig)
    stats: StatsConfig = field(default_factory=StatsConfig)
    plot: PlotConfig = field(default_factory=PlotConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    literature: LiteratureConfig = field(default_factory=LiteratureConfig)
    config_path: str | None = field(default=None, metadata={"internal": True})


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(data).__name__}")
    hints = get_type_hints(cls)
    public = [f for f in dataclasses.fields(cls) if not f.metadata.get("internal")]
    names = {f.name for f in public}
    unknown = sorted(set(data) - names)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(names)}")
    kwargs: dict[str, Any] = {}
    for f in public:
        if f.name not in data:
            continue
        hint = hints[f.name]
        value = data[f.name]
        if dataclasses.is_dataclass(hint):
            kwargs[f.name] = _build(hint, value or {}, f"{where}.{f.name}")
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """Return the public configuration as a plain nested dict (YAML-serialisable)."""
    d = dataclasses.asdict(cfg)
    d.pop("config_path", None)
    return d


def _resolve(base: Path, value: str | None) -> str | None:
    if value is None or value == "":
        return value
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())


def validate(cfg: Config) -> None:
    """Check value ranges and choices; raise :class:`ConfigError` on the first problem."""
    e = cfg.enhance
    if e.preset not in PRESET_NAMES:
        raise ConfigError(f"enhance.preset must be one of {PRESET_NAMES}, got {e.preset!r}")
    bad = [p for p in e.preview_presets if p not in PRESET_NAMES]
    if bad:
        raise ConfigError(f"enhance.preview_presets contains unknown preset(s) {bad}")
    if e.figure_default.output_size is not None and len(e.figure_default.output_size) != 2:
        raise ConfigError("enhance.figure_default.output_size must be [width, height] or null")
    tu = e.tone_unsharp
    if len(tu.source_percentiles) != len(tu.target_values):
        raise ConfigError("enhance.tone_unsharp: source_percentiles and target_values differ in length")
    if len(tu.amount_grid) != 3:
        raise ConfigError("enhance.tone_unsharp.amount_grid must be [start, stop, step]")
    s = cfg.segment
    if not 0 < s.downscale <= 1:
        raise ConfigError("segment.downscale must be in (0, 1]")
    if any(not 0 < d <= 1 for d in s.tune.downscale):
        raise ConfigError("segment.tune.downscale values must be in (0, 1]")
    if cfg.measure.shape_grid not in ("full", "segmentation"):
        raise ConfigError(f"measure.shape_grid must be 'full' or 'segmentation', got {cfg.measure.shape_grid!r}")
    sel = cfg.select
    if sel.connectivity not in (4, 8) or cfg.curate.connectivity not in (4, 8):
        raise ConfigError("connectivity must be 4 or 8")
    if not 0 <= sel.image_trim_fraction < 1:
        raise ConfigError("select.image_trim_fraction must be in [0, 1)")
    if cfg.review.background not in ("enhanced", "raw"):
        raise ConfigError("review.background must be 'enhanced' or 'raw'")
    v = cfg.vampire
    if not v.k or any(int(k) < 2 for k in v.k):
        raise ConfigError("vampire.k must be a non-empty list of integers >= 2")
    if v.elbow.k_min < 2 or v.elbow.k_max <= v.elbow.k_min:
        raise ConfigError("vampire.elbow needs 2 <= k_min < k_max")
    if v.renumber_by not in ("aspect_ratio", "none"):
        raise ConfigError("vampire.renumber_by must be 'aspect_ratio' or 'none'")
    pc = cfg.stats.pixel_calibration
    if (pc.field_width_um is None) != (pc.image_width_px is None):
        raise ConfigError("stats.pixel_calibration needs both field_width_um and image_width_px (or neither)")
    if pc.field_width_um is not None:
        if pc.field_width_um <= 0 or pc.image_width_px <= 0 or pc.binning <= 0:
            raise ConfigError("stats.pixel_calibration: field_width_um, image_width_px and binning must be > 0")
        if cfg.stats.pixel_size_um is not None:
            raise ConfigError("set either stats.pixel_size_um or stats.pixel_calibration, not both")
    elif pc.binning <= 0:
        raise ConfigError("stats.pixel_calibration.binning must be > 0")
    if cfg.stats.pixel_size_um is not None and cfg.stats.pixel_size_um <= 0:
        raise ConfigError("stats.pixel_size_um must be > 0 or null")
    if cfg.stats.significance_from not in ("p", "q"):
        raise ConfigError("stats.significance_from must be 'p' (uncorrected) or 'q' (BH-adjusted)")
    if cfg.stats.box.y_top not in ("whisker", "data"):
        raise ConfigError("stats.box.y_top must be 'whisker' or 'data'")
    if sorted(cfg.stats.star_thresholds, reverse=True) != list(cfg.stats.star_thresholds):
        raise ConfigError("stats.star_thresholds must be in decreasing order, e.g. [0.05, 0.01, 0.001]")
    u = cfg.ui
    if not 1 <= int(u.port) <= 65535:
        raise ConfigError("ui.port must be in 1..65535")
    if u.max_width < 64 or u.preview_width_px < 64:
        raise ConfigError("ui.max_width and ui.preview_width_px must be >= 64 px")
    if not 1 <= u.jpeg_quality <= 95 or not 1 <= u.llm.jpeg_quality <= 95:
        raise ConfigError("ui.jpeg_quality and ui.llm.jpeg_quality must be in 1..95")
    for key in ("median_target_range", "clahe_clip_range", "unsharp_amount_range"):
        r = getattr(u, key)
        if len(r) != 3 or not r[0] < r[1] or r[2] <= 0:
            raise ConfigError(f"ui.{key} must be [min, max, step] with min < max and step > 0")
    if u.card_table_max_rows < 1 or u.card_table_max_columns < 1:
        raise ConfigError("ui.card_table_max_rows and ui.card_table_max_columns must be >= 1")
    if u.llm.max_requests_per_minute < 1:
        raise ConfigError("ui.llm.max_requests_per_minute must be >= 1")
    if u.llm.enabled and u.llm.provider != "mock" and not u.llm.endpoint:
        raise ConfigError("ui.llm.enabled is true but ui.llm.endpoint is not set")
    cut = cfg.agent.cut
    if not cut.area_min_factors or any(f < 0 for f in cut.area_min_factors):
        raise ConfigError("agent.cut.area_min_factors must be a non-empty list of values >= 0")
    if not cut.frame_contact_factors or any(f < 0 for f in cut.frame_contact_factors):
        raise ConfigError("agent.cut.frame_contact_factors must be a non-empty list of values >= 0")
    if cut.histogram_bins < 5:
        raise ConfigError("agent.cut.histogram_bins must be >= 5")
    a = cfg.agent
    if a.thumbnail_width_px < 64:
        raise ConfigError("agent.thumbnail_width_px must be >= 64")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}", a.report.prefix or ""):
        raise ConfigError("agent.report.prefix must be 1-60 characters: letters, digits, '_', '.', '-'")
    if a.report.max_table_rows < 1 or a.report.max_number_columns < 0 or a.report.max_table_columns < 0:
        raise ConfigError("agent.report.max_table_rows must be >= 1; max_number_columns and max_table_columns >= 0")
    if not cfg.agent.output_subdir or Path(cfg.agent.output_subdir).is_absolute() or ".." in Path(cfg.agent.output_subdir).parts:
        raise ConfigError("agent.output_subdir must be a relative folder name inside output_dir")

    lit = cfg.literature
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", lit.tool_name or ""):
        raise ConfigError("literature.tool_name must be 1-40 characters: letters, digits, '_', '.', '-'")
    if lit.ncbi_requests_per_second is not None and not 0 < lit.ncbi_requests_per_second <= 10:
        raise ConfigError("literature.ncbi_requests_per_second must be in (0, 10] or null")
    if not 0 < lit.europepmc_requests_per_second <= 20:
        raise ConfigError("literature.europepmc_requests_per_second must be in (0, 20]")
    if lit.timeout_s <= 0 or lit.max_retries < 0 or lit.backoff_s < 0 or lit.cache_ttl_days < 0:
        raise ConfigError("literature: timeout_s > 0; max_retries, backoff_s and cache_ttl_days >= 0")
    if not 1 <= lit.max_results <= 200 or lit.methods_max_paragraphs < 1:
        raise ConfigError("literature.max_results must be in 1..200 and methods_max_paragraphs >= 1")
    if not 0 < lit.title_match_threshold <= 1:
        raise ConfigError("literature.title_match_threshold must be in (0, 1]")
    for key in ("eutils_url", "europepmc_url"):
        if not str(getattr(lit, key)).startswith("https://"):
            raise ConfigError(f"literature.{key} must be an https:// URL")


def load_config(path: str | Path | None) -> Config:
    """Load a YAML configuration and merge it over the defaults.

    Parameters
    ----------
    path : str or Path or None
        YAML file. ``None`` returns the defaults.

    Returns
    -------
    Config
        Validated configuration. Relative paths in ``output_dir``,
        ``input.image_dir``, ``curate.edits_csv``, ``vampire.src_path``,
        ``review.font_path``, ``plot.font_paths``, the preset data paths and the
        ``ui`` file paths (``html_path``, ``state_file``, ``llm.cache_file``) and
        ``agent.records_dir`` and ``literature.cache_dir`` are
        resolved relative to the directory of the YAML file.
    """
    if path is None:
        cfg = Config()
        validate(cfg)
        return cfg
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cfg = _build(Config, data, "config")
    base = path.resolve().parent
    cfg.output_dir = _resolve(base, cfg.output_dir)
    cfg.input.image_dir = _resolve(base, cfg.input.image_dir)
    cfg.curate.edits_csv = _resolve(base, cfg.curate.edits_csv)
    cfg.select.field_table = _resolve(base, cfg.select.field_table)
    cfg.vampire.src_path = _resolve(base, cfg.vampire.src_path)
    cfg.review.font_path = _resolve(base, cfg.review.font_path)
    cfg.plot.font_paths = [_resolve(base, p) for p in cfg.plot.font_paths]
    fd = cfg.enhance.figure_default
    fd.kernel_path = _resolve(base, fd.kernel_path)
    fd.tone_lut_path = _resolve(base, fd.tone_lut_path)
    cfg.ui.html_path = _resolve(base, cfg.ui.html_path)
    cfg.ui.state_file = _resolve(base, cfg.ui.state_file)
    cfg.ui.llm.cache_file = _resolve(base, cfg.ui.llm.cache_file)
    cfg.agent.records_dir = _resolve(base, cfg.agent.records_dir)
    cfg.literature.cache_dir = _resolve(base, cfg.literature.cache_dir)
    cfg.config_path = str(path.resolve())
    validate(cfg)
    return cfg


def dump_default_config(path: str | Path) -> None:
    """Write the default configuration as YAML (used by ``morphoagent init-config``)."""
    text = yaml.safe_dump(config_to_dict(Config()), sort_keys=False, allow_unicode=False)
    Path(path).write_text(text, encoding="utf-8")


def shape_grid_factor(cfg: Config) -> float | None:
    """Downscale factor for grid measurement of axis features, or ``None`` (``measure.shape_grid: full``)."""
    return cfg.segment.downscale if cfg.measure.shape_grid == "segmentation" else None


def pixel_size_um(cfg: Config) -> float | None:
    """Effective pixel size in micrometres per pixel, or ``None`` when not configured.

    Returns ``stats.pixel_size_um`` if set, otherwise
    ``field_width_um / image_width_px / binning`` from ``stats.pixel_calibration``
    (evaluated left to right in double precision, never rounded).
    """
    st = cfg.stats
    if st.pixel_size_um:
        return float(st.pixel_size_um)
    pc = st.pixel_calibration
    if pc.field_width_um is None or pc.image_width_px is None:
        return None
    return float(pc.field_width_um) / float(pc.image_width_px) / float(pc.binning)
