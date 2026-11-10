"""Field table: run step 6 on a chosen set of fields, optionally with given label files.

A field table is a CSV with one row per field (image) to analyse:

========== ========= =====================================================================
column     required  meaning
========== ========= =====================================================================
image      yes       image id from step 1 (``<condition>_<field>``)
label_file no        label TIFF to use instead of the step-5 labels (empty = step 5);
                     relative paths are resolved against the folder of the CSV
condition  no        if given, must equal the condition parsed in step 1 (a guard
                     against a table written for another image set)
shape_grid no        ``full`` or ``segmentation``: overrides ``measure.shape_grid`` for
                     this field (empty = the configured value)
segmentation_label_file
           no        the segmentation output the label file was derived from; in grid
                     mode, objects whose mask differs from it are measured at full
                     resolution (empty = treat the label file as segmentation output)
========== ========= =====================================================================

Other columns are allowed and ignored (for example a note on why a field was chosen).
Typical uses: reproduce an analysis whose field set and hand-corrected label files were
chosen by a person, or analyse labels produced by another segmentation tool.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import tifffile

REQUIRED = ("image",)
SHAPE_GRID_VALUES = ("", "full", "segmentation")
SOURCE_SEGMENT = "segment"
SOURCE_FILE = "field_table"


class FieldTableError(ValueError):
    """The field table cannot be used with this image set."""


def read_field_table(path: str | Path, inventory: pd.DataFrame) -> pd.DataFrame:
    """Read and check a field table against the step-1 inventory.

    Parameters
    ----------
    path : str or pathlib.Path
        CSV file.
    inventory : pandas.DataFrame
        Valid images of step 1 (columns ``image``, ``condition``, ``field``).

    Returns
    -------
    pandas.DataFrame
        Columns ``image``, ``condition``, ``field``, ``label_file`` (absolute path or
        ``''``), ``label_source`` (``segment`` or ``field_table``), ``shape_grid``
        (``''``, ``full`` or ``segmentation``) and ``segmentation_label_file`` (absolute
        path or ``''``), in the row order of the inventory.

    Raises
    ------
    FieldTableError
        Missing ``image`` column, empty table, duplicated or unknown images, condition
        mismatch, a label file that does not exist, or an unknown ``shape_grid`` value.
    """
    path = Path(path)
    if not path.is_file():
        raise FieldTableError(f"field table not found: {path}")
    ft = pd.read_csv(path, dtype=str, keep_default_na=False)
    ft.columns = [c.strip() for c in ft.columns]
    missing = [c for c in REQUIRED if c not in ft.columns]
    if missing:
        raise FieldTableError(f"field table {path.name}: missing column(s) {missing}")
    ft["image"] = ft["image"].str.strip()
    ft = ft[ft["image"] != ""]
    if ft.empty:
        raise FieldTableError(f"field table {path.name} lists no images")
    dup = sorted(ft.loc[ft["image"].duplicated(), "image"].unique())
    if dup:
        raise FieldTableError(f"field table {path.name}: image(s) listed more than once: {dup}")
    inv = inventory.set_index("image")
    unknown = [im for im in ft["image"] if im not in inv.index]
    if unknown:
        raise FieldTableError(
            f"field table {path.name}: {len(unknown)} image id(s) not among the valid images of step 1, "
            f"e.g. {unknown[:5]} (ids are '<condition>_<field>' after strip_prefix / strip_suffix)")
    if "condition" in ft.columns:
        given = ft.set_index("image")["condition"].str.strip()
        bad = [im for im, c in given.items() if c and c != inv.loc[im, "condition"]]
        if bad:
            raise FieldTableError(f"field table {path.name}: condition differs from step 1 for {bad[:5]}")
    def column(name: str) -> pd.Series:
        return ft[name].str.strip() if name in ft.columns else pd.Series("", index=ft.index)

    def resolve(values: pd.Series, what: str) -> list[str]:
        out_paths = []
        for im, lf in zip(ft["image"], values, strict=True):
            if not lf:
                out_paths.append("")
                continue
            p = Path(lf).expanduser()
            p = p if p.is_absolute() else (path.parent / p)
            if not p.is_file():
                raise FieldTableError(f"field table {path.name}: {what} for {im} not found: {p}")
            out_paths.append(str(p.resolve()))
        return out_paths

    grid = column("shape_grid").str.lower()
    bad_grid = sorted(set(grid) - set(SHAPE_GRID_VALUES))
    if bad_grid:
        raise FieldTableError(f"field table {path.name}: shape_grid must be empty, 'full' or 'segmentation', got {bad_grid}")
    out = pd.DataFrame({"image": ft["image"].to_numpy(), "label_file": resolve(column("label_file"), "label file")})
    out["label_source"] = np.where(out.label_file != "", SOURCE_FILE, SOURCE_SEGMENT)
    out["shape_grid"] = grid.to_numpy()
    out["segmentation_label_file"] = resolve(column("segmentation_label_file"), "segmentation label file")
    order = {im: i for i, im in enumerate(inventory["image"])}
    out["_o"] = out.image.map(order)
    out = out.sort_values("_o").drop(columns="_o").reset_index(drop=True)
    out.insert(1, "condition", out.image.map(inv["condition"]))
    out.insert(2, "field", out.image.map(inv["field"]))
    return out


def check_label_files(table: pd.DataFrame, inventory: pd.DataFrame) -> None:
    """Check size and data type of every given label file from its TIFF header (before any output is written)."""
    inv = inventory.set_index("image")
    seg_files = table["segmentation_label_file"] if "segmentation_label_file" in table else pd.Series("", index=table.index)
    pairs = list(zip(table.image, table.label_file, strict=True)) + list(zip(table.image, seg_files, strict=True))
    for im, lf in pairs:
        if not lf:
            continue
        with tifffile.TiffFile(lf) as tf:
            page = tf.pages[0]
            shape, dtype = tuple(page.shape), np.dtype(page.dtype)
        want = (int(inv.loc[im, "height"]), int(inv.loc[im, "width"]))
        if shape != want:
            raise FieldTableError(f"label file {lf}: size {shape} differs from the image size {want} of {im}")
        if not np.issubdtype(dtype, np.integer):
            raise FieldTableError(f"label file {lf}: labels must be integers, got {dtype}")


def read_label_file(path: str | Path, shape: tuple[int, int]) -> np.ndarray:
    """Read a label TIFF and check it has the image size and integer labels.

    Returns the labels as int32 (0 = background).
    """
    lab = tifffile.imread(str(path))
    if lab.ndim != 2:
        raise FieldTableError(f"label file {path}: expected a 2-D label image, got shape {lab.shape}")
    if tuple(lab.shape) != tuple(shape):
        raise FieldTableError(f"label file {path}: size {lab.shape} differs from the image size {tuple(shape)}")
    if not np.issubdtype(lab.dtype, np.integer):
        raise FieldTableError(f"label file {path}: labels must be integers, got {lab.dtype}")
    if lab.min() < 0:
        raise FieldTableError(f"label file {path}: negative label values")
    return lab.astype(np.int32)
