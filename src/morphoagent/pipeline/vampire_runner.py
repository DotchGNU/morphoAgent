"""Standalone VAMPIRE runner (executed by the VAMPIRE Python interpreter).

Usage::

    python vampire_runner.py job.json

This file is deliberately self-contained: it imports only the standard library,
numpy, pandas and Pillow at module level and must stay compatible with
Python 3.8 and pandas 0.25, because VAMPIRE_open (Phillip et al., Nat Protoc
2021, doi:10.1038/s41596-020-00432-x) is usually installed in its own older
environment. VAMPIRE itself (and its dependencies: scikit-learn, OpenCV,
SciPy, matplotlib, tkinter) is imported from ``job["vampire_src"]`` at run time.
No VAMPIRE file is copied or modified.

Why VAMPIRE internals are wrapped (monkey-patched in memory)
------------------------------------------------------------
VAMPIRE's public entry points (``getboundary``, ``mainbody``) write tables and
figures but do not return

* the normalised feature matrix that its k-means is fitted on (PC scores,
  Box-Cox transformed, z-scored, then L2-normalised per object), which the elbow
  analysis must use so that inertia values are comparable to VAMPIRE's own fit;
* the PCA eigenvalues;
* the registered object contours and the per-object mode index, needed for the
  mean contour of each shape mode.

The runner therefore wraps, in memory only, ``clusterSM.KMeans`` (records the
matrix passed to ``fit`` and the fitted parameters / inertia),
``pca_bdreg.PCA_custom`` (records eigenvalues) and ``mainbody.clusterSM``
(records the registered contours and the mode index of build and apply runs).
The wrapped functions call the originals unchanged, so VAMPIRE's results are
identical to an unwrapped run. Two GUI/OS details are also neutralised:
``matplotlib.use`` (VAMPIRE requests the TkAgg backend; the Agg backend is used
instead) and the Windows-only ``attrib +H`` call in ``getboundary``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import Image


class _Var:
    """Minimal stand-in for the tkinter Entry widgets VAMPIRE reads from."""

    def __init__(self, v=""):
        self.v = str(v)

    def get(self):
        return self.v

    def delete(self, *args):
        self.v = ""

    def insert(self, index, s):
        self.v = str(s)
        print("[vampire status]", s, flush=True)


class _Bar(dict):
    """Minimal stand-in for the tkinter progress bar."""

    def update(self):
        pass


def _install_wrappers(src):
    """Import VAMPIRE modules from ``src`` with the recording wrappers installed."""
    import matplotlib

    matplotlib.use("Agg")
    matplotlib.use = lambda *a, **k: None  # VAMPIRE asks for TkAgg at import time
    import subprocess as sp

    real_check_call = sp.check_call

    def _check_call(cmd, *a, **k):
        if isinstance(cmd, list) and cmd and cmd[0] == "attrib":  # Windows-only hide-file call
            return 0
        return real_check_call(cmd, *a, **k)

    sp.check_call = _check_call
    sys.path.insert(0, src)
    import clusterSM as CS  # noqa: N812
    import getboundary as GB  # noqa: N812
    import mainbody as MB  # noqa: N812
    import pca_bdreg as PB  # noqa: N812

    rec = {}
    base_kmeans = CS.KMeans

    class RecKMeans(base_kmeans):
        def fit(self, X, y=None, sample_weight=None):  # noqa: N803
            r = super().fit(X)
            rec["kmeans"] = {"X": np.array(X), "params": self.get_params(), "inertia": float(self.inertia_)}
            return r

    CS.KMeans = RecKMeans
    base_pca = PB.PCA_custom

    def rec_pca(data, *a, **k):
        pc, score, latent = base_pca(data, *a, **k)
        rec["pca_latent"] = np.array(latent)
        return pc, score, latent

    PB.PCA_custom = rec_pca
    base_csm = MB.clusterSM

    def rec_csm(outpth, score, bdpc, clnum, pcnum, model, build, *a, **k):
        out = base_csm(outpth, score, bdpc, clnum, pcnum, model, build, *a, **k)
        if build:
            rec["build"] = {"IDX": np.array(out[0]), "bdpc": np.array(bdpc),
                            "pcnum": pcnum if pcnum is not None else model["pcnum"]}
        else:
            rec.setdefault("apply", []).append(np.array(out[0]))
        return out

    MB.clusterSM = rec_csm
    return GB, MB, rec


def _label_map(path):
    """Sorted non-zero label values of a label TIFF (VAMPIRE ObjectID k -> k-th label)."""
    a = np.array(Image.open(path))
    return sorted(int(v) for v in np.unique(a) if v != 0)


def main(job_path):
    with open(job_path) as fh:
        job = json.load(fh)
    work = Path(job["work_dir"])
    sets_csv = str(work / "image_sets.csv")
    files = pd.read_csv(work / "file_map.csv")  # filename, condition, image, set_location
    n_coord = int(job["n_coordinates"])
    t0 = time.time()
    GB, MB, rec = _install_wrappers(job["vampire_src"])
    entries = {"Number of coordinates": _Var(n_coord), "Model name": _Var("m"), "Status": _Var(),
               "Model to apply": _Var("")}
    bar = _Bar()
    sets = pd.read_csv(sets_csv)
    GB.getboundary(sets_csv, bar, entries)
    print("[runner] getboundary done in %.0f s" % (time.time() - t0), flush=True)

    lab_maps = {}
    for _, r in files.iterrows():
        lab_maps[r["filename"]] = _label_map(os.path.join(r["set_location"], r["filename"]))
    file_image = dict(zip(files.filename, files.image))
    X_ref, meta_k = None, {}
    for K in job["k"]:
        kdir = work / "runs" / ("k%d" % K)
        model_name = "%s_k%d" % (job["model_name"], K)
        (kdir / "model" / model_name).mkdir(parents=True, exist_ok=True)
        (kdir / "result").mkdir(parents=True, exist_ok=True)
        entries["Model name"] = _Var(model_name)
        entries["Model to apply"] = _Var(str(kdir / "model" / model_name / (model_name + ".pickle")))
        rec.pop("apply", None)
        t = time.time()
        MB.mainbody(True, sets_csv, entries, str(kdir / "model"), str(K), bar)
        tb = time.time() - t
        t = time.time()
        MB.mainbody(False, sets_csv, entries, str(kdir / "result"), None, bar)
        ta = time.time() - t
        print("[runner] K=%d build %.0f s, apply %.0f s" % (K, tb, ta), flush=True)
        frames = []
        for _, s in sets.iterrows():
            sh = pd.read_csv(os.path.join(s["set location"], "VAMPIRE datasheet %s.csv" % s["tag"]))
            sh.insert(0, "condition", s["condition"])
            frames.append(sh)
        D = pd.concat(frames, ignore_index=True)
        D["image"] = D.Filename.map(file_image)
        D["label"] = [lab_maps[f][int(o) - 1] for f, o in zip(D.Filename, D.ObjectID)]
        out = pd.DataFrame({
            "condition": D.condition, "image": D.image, "label": D.label, "vampire_object_id": D.ObjectID,
            "raw_shape_mode": D["Shape mode"].astype(int), "dist_from_center": D["Distance from cluster center"],
            "vampire_area_px": D.Area, "vampire_filename": D.Filename,
        })
        out.to_csv(kdir / "cells_vampire_raw.csv", index=False)
        b = rec["build"]
        apply_idx = np.concatenate(rec["apply"]).astype(int)
        bd = b["bdpc"]
        base = 0 if apply_idx.min() == 0 else 1
        rows = []
        for m in range(1, K + 1):
            sel = apply_idx == (m - 1 + base)
            mb = bd[sel].mean(axis=0) if sel.any() else np.full(bd.shape[1], np.nan)
            for j in range(n_coord):
                rows.append({"raw_shape_mode": m, "point": j + 1, "x": mb[j], "y": mb[n_coord + j], "n_objects": int(sel.sum())})
        pd.DataFrame(rows).to_csv(kdir / "mode_mean_contour_raw.csv", index=False)
        km = rec["kmeans"]
        X = km["X"]
        if X_ref is None:
            X_ref = X
        same_x = bool(X.shape == X_ref.shape and np.allclose(X, X_ref))
        params = {k: (v if isinstance(v, (int, float, str, type(None))) else str(v)) for k, v in km["params"].items()}
        meta_k[str(K)] = {
            "n_objects_datasheet": int(len(D)), "apply_idx_base": int(base), "pcnum_used": int(b["pcnum"]),
            "kmeans_params_recorded": params, "kmeans_inertia_recorded": km["inertia"],
            "build_vs_apply_mode_agreement": float((apply_idx == b["IDX"]).mean()) if len(apply_idx) == len(b["IDX"]) else None,
            "normalised_matrix_identical_to_first_k": same_x, "build_s": round(tb, 1), "apply_s": round(ta, 1),
        }
    np.save(work / "normalised_features.npy", X_ref)
    np.save(work / "pca_eigenvalues.npy", rec["pca_latent"])

    from sklearn.cluster import KMeans  # available wherever VAMPIRE runs

    kc = job["elbow_kmeans"]
    rows = []
    ks = list(range(int(job["elbow_k_min"]), int(job["elbow_k_max"]) + 1))
    for i, K in enumerate(ks, 1):
        km = KMeans(n_clusters=K, init=kc["init"], n_init=kc["n_init"], max_iter=kc["max_iter"],
                    random_state=kc["random_state"]).fit(X_ref)
        rows.append({"K": K, "inertia": float(km.inertia_), "n_iter": int(km.n_iter_)})
        sys.stdout.write("\r[runner] elbow K=%d (%d/%d) inertia=%.3f   " % (K, i, len(ks), km.inertia_))
        sys.stdout.flush()
    print("", flush=True)
    pd.DataFrame(rows).to_csv(work / "elbow_raw.csv", index=False)
    import sklearn

    ev = rec["pca_latent"]
    cum = np.cumsum(ev) / ev.sum()
    first = meta_k[str(job["k"][0])]
    meta = {
        "vampire_src": job["vampire_src"], "python": sys.version.split()[0], "numpy": np.__version__,
        "pandas": pd.__version__, "scikit-learn": sklearn.__version__, "n_coordinates": n_coord,
        "pcnum_used": first["pcnum_used"], "cumulative_variance_at_pcnum": float(cum[first["pcnum_used"] - 1]),
        "normalised_matrix_shape": list(X_ref.shape), "per_k": meta_k, "elapsed_s": round(time.time() - t0, 1),
    }
    with open(work / "runner_meta.json", "w") as fh:
        json.dump(meta, fh, indent=2)
    print("[runner] done in %.0f s" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python vampire_runner.py job.json")
    main(sys.argv[1])
