"""Agent tools of stages 0-3 (morphoagent.mcp) on synthetic images and label images."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import tifffile
import yaml

from morphoagent.mcp import stage_tools as st
from morphoagent.pipeline.common import OutputExistsError
from morphoagent.pipeline.config import load_config

SHAPE = (240, 320)


def _hashes(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


def _rect(lab: np.ndarray, value: int, r0: int, r1: int, c0: int, c1: int) -> None:
    lab[r0:r1, c0:c1] = value


@pytest.fixture
def label_config(tmp_path: Path) -> Path:
    """Config whose step-5 output is written by hand from synthetic label images.

    CTRL_Image001  label 1: 10x10 = 100 px^2, interior
                   label 2: 20x20 = 400 px^2, interior
                   label 3: 30x10 = 300 px^2, 10 px on the top frame row
    DRUG_Image001  label 1: 30x30 = 900 px^2, interior
                   label 2: two pieces 10x20 = 200 and 5x10 = 50 (largest piece keeps 200)
    """
    out = tmp_path / "out"
    seg = out / "05_segment"
    (seg / "labels").mkdir(parents=True)
    a = np.zeros(SHAPE, np.uint16)
    _rect(a, 1, 100, 110, 100, 110)
    _rect(a, 2, 150, 170, 150, 170)
    _rect(a, 3, 0, 30, 50, 60)
    b = np.zeros(SHAPE, np.uint16)
    _rect(b, 1, 50, 80, 50, 80)
    _rect(b, 2, 150, 160, 200, 220)
    _rect(b, 2, 200, 205, 200, 210)
    from morphoagent.pipeline.segmentation import measure_objects

    objs, imgs = [], []
    for cond, lab in (("CTRL", a), ("DRUG", b)):
        image = f"{cond}_Image001"
        tifffile.imwrite(seg / "labels" / f"{image}_labels.tif", lab)
        m = measure_objects(lab)
        m.insert(0, "field", "Image001")
        m.insert(0, "condition", cond)
        m.insert(0, "image", image)
        objs.append(m)
        imgs.append({"image": image, "condition": cond, "field": "Image001", "n_objects": len(m), "runtime_s": 0.0,
                     "height": SHAPE[0], "width": SHAPE[1]})
    pd.concat(objs).to_csv(seg / "objects.csv", index=False)
    pd.DataFrame(imgs).to_csv(seg / "images.csv", index=False)
    (seg / "run_manifest.json").write_text(json.dumps({"step": 5}))
    cfg = {"output_dir": str(out), "conditions": {"order": ["CTRL", "DRUG"]},
           "select": {"area_min_px": 150, "frame_contact_px": 5}}
    p = tmp_path / "labels.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


# --------------------------------------------------------------------------- #
# Stage 0
# --------------------------------------------------------------------------- #
def test_inspect_folder_counts_conditions_and_writes_nothing(image_dir: Path) -> None:
    before = _hashes(image_dir)
    r = st.inspect_folder(str(image_dir), strip_prefix="exp01_", strip_suffix="_ch00")
    assert _hashes(image_dir) == before
    assert (r["n_files"], r["n_matched"], r["n_unmatched"]) == (7, 7, 0)
    assert r["conditions"] == [{"condition": "CTRL", "n_images": 4, "n_all_zero": 1},
                               {"condition": "DRUG_A", "n_images": 3, "n_all_zero": 0}]
    assert r["all_zero_files"] == ["exp01_CTRL_Image099_ch00.png"]
    assert r["image_sizes"] == [{"width": 320, "height": 240, "n_images": 7}]
    assert r["pixel_modes"] == {"L": 7}
    assert r["config_snippet"]["conditions"]["order"] == ["CTRL", "DRUG_A"]
    assert r["config_snippet"]["input"]["strip_prefix"] == "exp01_"
    json.dumps(r)  # JSON-serialisable


def test_inspect_folder_reports_unmatched_names(image_dir: Path) -> None:
    r = st.inspect_folder(str(image_dir), read_pixels=False)  # default regex, suffix '_ch00' not stripped
    assert r["n_matched"] == 0 and r["n_unmatched"] == 7
    assert r["config_snippet"] is None and r["warnings"]


def test_inspect_folder_rejects_bad_input(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        st.inspect_folder(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="named groups"):
        st.inspect_folder(str(tmp_path), name_regex=r"^(?P<x>.+)$")


# --------------------------------------------------------------------------- #
# Stage 1
# --------------------------------------------------------------------------- #
def test_preview_enhance_runs_load_and_refuses_overwrite(config_file: Path, image_dir: Path) -> None:
    before = _hashes(image_dir)
    r = st.preview_enhance(str(config_file), presets=["figure-default", "none"])
    assert r["ran_load"] is True
    assert Path(r["contact_sheet"]).is_file() and Path(r["preview_metrics_csv"]).is_file()
    assert [p["preset"] for p in r["per_preset"]] == ["figure-default", "none"]
    assert len(r["images"]) == 2 * len(r["sample_images"])
    with pytest.raises(OutputExistsError):
        st.preview_enhance(str(config_file))
    r2 = st.preview_enhance(str(config_file), overwrite=True)
    assert r2["ran_load"] is False and len(r2["per_preset"]) == 4
    assert _hashes(image_dir) == before
    with pytest.raises(ValueError, match="unknown preset"):
        st.preview_enhance(str(config_file), presets=["magic"], overwrite=True)


# --------------------------------------------------------------------------- #
# Stage 2
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("fake_cellpose")
def test_run_segment_tune_and_all(config_file: Path) -> None:
    t = st.run_segment(str(config_file), mode="tune")
    assert t["ran_load"] is True and len(t["montages"]) == 2
    assert sum(s["is_default"] for s in t["per_setting"]) == 1
    a = st.run_segment(str(config_file), mode="all")
    assert a["n_images"] == 6 and a["n_objects"] == sum(a["objects_per_condition"].values())
    assert len(list(Path(a["labels_dir"]).glob("*.tif"))) == 6
    with pytest.raises(OutputExistsError):
        st.run_segment(str(config_file), mode="all")
    with pytest.raises(ValueError):
        st.run_segment(str(config_file), mode="everything")


def test_cut_distributions_on_synthetic_labels(label_config: Path) -> None:
    r = st.cut_distributions(str(label_config), area_min_px=[150, 250], frame_contact_px=[5, 20])
    assert r["source_step"] == 5 and r["n_objects"] == 5  # the 50 px^2 piece is removed by the largest-piece rule
    assert r["area_min_px_candidates"] == [150.0, 250.0] and r["frame_contact_px_candidates"] == [5.0, 20.0]
    kept = {(x["area_min_px"], x["frame_contact_px"], x["condition"]): x["n_kept"] for x in r["rows"]}
    assert kept == {
        (150.0, 5.0, "CTRL"): 1, (150.0, 5.0, "DRUG"): 2,
        (150.0, 20.0, "CTRL"): 2, (150.0, 20.0, "DRUG"): 2,
        (250.0, 5.0, "CTRL"): 1, (250.0, 5.0, "DRUG"): 1,
        (250.0, 20.0, "CTRL"): 2, (250.0, 20.0, "DRUG"): 1,
    }
    row = next(x for x in r["rows"] if (x["area_min_px"], x["frame_contact_px"], x["condition"]) == (150.0, 5.0, "CTRL"))
    assert (row["n_below_area_min"], row["n_frame_contact"], row["n_kept"], row["n_objects_in"]) == (1, 1, 1, 3)
    assert row["is_current"] is True
    out = Path(r["out_dir"])
    assert out.parent == Path(load_config(label_config).output_dir) / "agent"
    for name in ("cut_table.csv", "cut_kept_pivot.csv", "cut_histograms.png", "cut_histograms.svg",
                 "source_cut_histograms.csv", "run_manifest.json"):
        assert (out / name).is_file(), name
    src = pd.read_csv(out / "source_cut_histograms.csv")
    bins = src[src.row_type == "bin"]
    assert bins.groupby("panel")["count"].sum().to_dict() == {"border_contact_px": 5, "log10_area_px": 5}
    assert set(src[src.row_type == "cutoff"].x_left.round(6)) >= {5.0, 20.0}
    pivot = pd.read_csv(out / "cut_kept_pivot.csv")
    assert list(pivot.columns) == ["area_min_px", "frame_contact_px", "CTRL", "DRUG", "all_conditions"]
    labels_before = _hashes(Path(load_config(label_config).output_dir) / "05_segment" / "labels")
    with pytest.raises(OutputExistsError):
        st.cut_distributions(str(label_config), area_min_px=[150, 250], frame_contact_px=[5, 20])
    st.cut_distributions(str(label_config), area_min_px=[150, 250], frame_contact_px=[5, 20], overwrite=True)
    assert _hashes(Path(load_config(label_config).output_dir) / "05_segment" / "labels") == labels_before


def test_cut_distributions_default_candidates_follow_config(label_config: Path) -> None:
    r = st.cut_distributions(str(label_config))
    cfg = load_config(label_config)
    assert r["area_min_px_candidates"] == sorted({f * 150.0 for f in cfg.agent.cut.area_min_factors})
    assert r["frame_contact_px_candidates"] == sorted({f * 5.0 for f in cfg.agent.cut.frame_contact_factors})
    assert r["current"] == {"area_min_px": 150.0, "frame_contact_px": 5.0}
    assert len(r["rows"]) == len(r["area_min_px_candidates"]) * len(r["frame_contact_px_candidates"]) * 2


@pytest.mark.usefixtures("fake_cellpose")
def test_cut_distributions_matches_step6(config_file: Path) -> None:
    from morphoagent.pipeline.cli import main

    assert main(["run", "-c", str(config_file), "--from", "1", "--to", "1"]) == 0
    assert main(["run", "-c", str(config_file), "--from", "5", "--to", "5"]) == 0
    cfg = load_config(config_file)
    before = st.cut_distributions(str(config_file), [cfg.select.area_min_px], [cfg.select.frame_contact_px])
    assert before["source_step"] == 5
    assert main(["select", "-c", str(config_file)]) == 0
    after = st.cut_distributions(str(config_file), [cfg.select.area_min_px], [cfg.select.frame_contact_px])
    assert after["source_step"] == 6
    cells = pd.read_csv(Path(cfg.output_dir) / "06_select" / "cells.csv").groupby("condition").size().to_dict()
    for res in (before, after):
        assert {x["condition"]: x["n_kept"] for x in res["rows"]} == cells


# --------------------------------------------------------------------------- #
# Stage 3
# --------------------------------------------------------------------------- #
def test_run_vampire_without_checkout_fails_before_writing(label_config: Path) -> None:
    with pytest.raises(FileNotFoundError, match="VAMPIRE_open"):
        st.run_vampire(str(label_config))
    assert not (Path(load_config(label_config).output_dir) / "09_vampire").exists()
    with pytest.raises(ValueError, match=">= 2"):
        st.run_vampire(str(label_config), k=[1])


VAMPIRE_SRC = os.environ.get("MORPHOAGENT_TEST_VAMPIRE_SRC")
VAMPIRE_PY = os.environ.get("MORPHOAGENT_TEST_VAMPIRE_PYTHON")


@pytest.mark.skipif(not (VAMPIRE_SRC and VAMPIRE_PY),
                    reason="set MORPHOAGENT_TEST_VAMPIRE_SRC and MORPHOAGENT_TEST_VAMPIRE_PYTHON to run VAMPIRE")
@pytest.mark.usefixtures("fake_cellpose")
def test_run_vampire_with_external_checkout(config_file: Path) -> None:
    from morphoagent.pipeline.cli import main

    text = config_file.read_text() + yaml.safe_dump(
        {"vampire": {"src_path": VAMPIRE_SRC, "python": VAMPIRE_PY, "elbow": {"k_min": 2, "k_max": 4}}})
    config_file.write_text(text)
    for n in (1, 5, 6):
        assert main(["run", "-c", str(config_file), "--from", str(n), "--to", str(n)]) == 0
    r = st.run_vampire(str(config_file), k=[2, 3])
    assert r["k"] == [2, 3] and set(r["per_k"]) == {"K2", "K3"}
    assert r["elbow"]["k_second_diff"] in (2, 3, 4)
    assert any(f.endswith("mode_mean_contours_K2.png") for f in r["figures"])


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #
def test_server_registers_typed_tools(image_dir: Path) -> None:
    pytest.importorskip("mcp")
    from morphoagent.mcp.pipeline_server import server

    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"inspect_folder", "preview_enhance", "run_segment", "cut_distributions", "run_vampire",
                          "propose", "list_pending", "get_decision", "build_report"}
    assert tools["inspect_folder"].annotations.read_only_hint is True
    assert tools["run_segment"].input_schema["properties"]["mode"]["enum"] == ["tune", "all"]
    for name in ("preview_enhance", "run_segment", "cut_distributions", "run_vampire"):
        assert "config_path" in tools[name].input_schema["required"]
        assert tools[name].input_schema["properties"]["overwrite"]["default"] is False
    res = asyncio.run(server.call_tool("inspect_folder", {"path": str(image_dir), "strip_prefix": "exp01_",
                                                           "strip_suffix": "_ch00"}))
    assert not res.is_error
    assert res.structured_content["n_matched"] == 7
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    with pytest.raises(ToolError, match="not a folder") as info:  # anticipated: message reaches the client
        asyncio.run(server.call_tool("inspect_folder", {"path": str(image_dir / "missing")}))
    assert not isinstance(info.value, UnexpectedToolError)


def test_server_over_stdio(image_dir: Path) -> None:
    pytest.importorskip("mcp")
    import sys

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "morphoagent.mcp.pipeline_server"],
                                   env={"PYTHONNOUSERSITE": "1"})

    async def go() -> tuple[list[str], dict, bool, str]:
        async with Client(params) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            ok = await client.call_tool("inspect_folder", {"path": str(image_dir), "strip_prefix": "exp01_",
                                                            "strip_suffix": "_ch00", "read_pixels": False})
            bad = await client.call_tool("cut_distributions", {"config_path": str(image_dir / "none.yaml")})
            return names, ok.structured_content, bool(bad.is_error), bad.content[0].text

    names, ok, bad_is_error, bad_text = asyncio.run(go())
    assert names == ["build_report", "cut_distributions", "get_decision", "inspect_folder", "list_pending",
                     "preview_enhance", "propose", "run_segment", "run_vampire"]
    assert ok["n_matched"] == 7 and ok["conditions"][0] == {"condition": "CTRL", "n_images": 4}
    assert bad_is_error and "configuration file not found" in bad_text
