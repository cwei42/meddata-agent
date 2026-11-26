"""Structure / MHA toolkit (deterministic, SimpleITK + numpy). Works on an MHA export folder (cli/to_mha.py)
or on plain image/mask files.

    ex = Export("/data/x_mha")                      # reads structures.json + image sidecars
    gtv = ex.find("GTV*", patient="P001")            # by TG-263 name, original name or glob; helpers (z...) excluded
    m = combine([ex.mask(s) for s in gtv])           # union | intersection | minus
    m5 = margin(m, 5)                                # +5 mm (negative = shrink, (x, y, z) = anisotropic)
    structure_stats(m5)  compare(a, b)  image_stats(ct, m)  dose_stats(dose, m)  dvh(dose, m)
    labelmap([("GTV", m), ("PTV", p)])  resample(img, spacing=(1, 1, 1))  crop(img, m, 10)

All masks are UInt8 0/1 images with full geometry. Images on other grids are resampled onto the mask grid
(masks: nearest neighbour, images/dose: linear) and this is reported.
"""
import fnmatch
import glob
import json
import os
import re

import numpy as np
import SimpleITK as sitk

from . import structnames as N


def _is_oar(s):
    """an organ: a recognised TG-263 name that is not a target, the body outline, a helper or a point"""
    std = s.get("standard", "")
    return s.get("naming") in ("standard", "synonym", "mapping") and std not in ("External", "Body") \
        and not std.lower().startswith("z")


def _is_target(s):
    return s.get("naming") == "target"


# select='OARS' / 'TARGETS' picks structures by class (names that could not be classified are in neither)
CATEGORIES = {N.key(k): f for k, f in [("OARS", _is_oar), ("OAR", _is_oar), ("organs", _is_oar),
                                        ("organs at risk", _is_oar), ("TARGETS", _is_target)]}


# ------------------------------------------------------------------ export folder
class Export:
    def __init__(self, folder, derived=None):
        """derived: folder with structures created earlier (merge/margin results, <patient>/<name>.mha + .json);
        they can be selected like the exported ones (naming 'derived')."""
        self.folder = os.path.abspath(os.path.expanduser(folder))
        if not os.path.isdir(self.folder):
            raise FileNotFoundError(f"MHA export not found: {self.folder} (run the MHA export first)")
        self.structures, self.images = [], []
        for js in sorted(glob.glob(os.path.join(self.folder, "**", "structures.json"), recursive=True)):
            d = json.load(open(js, encoding="utf-8"))
            base = os.path.dirname(js)
            ref = d.get("reference_image_mha")
            ref_id = (d.get("reference") or "").replace("series ", "") if str(d.get("reference", "")).startswith(
                "series ") else None
            for s in d.get("structures", []):
                self.structures.append({
                    "patient": d.get("rtstruct", "").split("-")[0], "rtstruct": d.get("rtstruct", ""),
                    "name": s.get("name", ""), "standard": s.get("standard", s.get("name", "")),
                    "naming": s.get("naming", ""), "file": os.path.join(base, s["file"]),
                    "reference_mha": os.path.normpath(os.path.join(base, ref)) if ref else None,
                    "reference_id": ref_id})
        for js in sorted(glob.glob(os.path.join(self.folder, "**", "*.json"), recursive=True)):
            if os.path.basename(js) == "structures.json" or not os.path.exists(js[:-5] + ".mha"):
                continue
            try:
                d = json.load(open(js, encoding="utf-8"))
            except Exception:
                continue
            self.images.append({"id": d.get("id", ""), "patient": d.get("id", "").split("-")[0],
                                "modality": d.get("modality", ""), "description": d.get("description", ""),
                                "file": js[:-5] + ".mha"})
        if derived and os.path.isdir(derived):
            for js in sorted(glob.glob(os.path.join(derived, "*", "*.json"))):
                try:
                    d = json.load(open(js, encoding="utf-8"))
                except Exception:
                    continue
                if "operation" not in d or not os.path.exists(js[:-5] + ".mha"):
                    continue                                  # only merge/margin results (not label maps)
                self.structures.append({
                    "patient": d.get("patient", ""), "rtstruct": "derived", "name": d.get("name", ""),
                    "standard": d.get("name", ""), "naming": "derived", "file": js[:-5] + ".mha",
                    "reference_mha": None, "reference_id": d.get("reference_series")})

    def find(self, query, patient=None, rtstruct=None, include_helpers=False):
        """query: exact TG-263 / original name (case, spaces, umlauts ignored) or glob ('GTV*', 'Parotid_?'),
        several separated by ','. Helper structures (z..., ring/opt) only with include_helpers."""
        out = []
        for q in [x.strip() for x in str(query).split(",") if x.strip()]:
            cat = CATEGORIES.get(N.key(q))
            if cat:                     # 'OARS' / 'TARGETS': by the TG-263 classification, not by name
                for s in self.structures:
                    if patient and s["patient"] != patient or rtstruct and s["rtstruct"] != rtstruct:
                        continue
                    if cat(s) and s not in out:
                        out.append(s)
                continue
            glob_q = any(c in q for c in "*?[")
            # plain words or synonyms ("brachial plexus", "Rueckenmark", "Lunge") -> the TG-263 base name;
            # a paired organ named without side matches both sides
            base = None
            if not glob_q:
                std = N.standardize(q)
                base = std["standard"] if std["status"] in ("synonym", "standard") else \
                    (N.SYNONYMS.get(N.key(q)) or (None,))[0]
            for s in self.structures:
                if patient and s["patient"] != patient or rtstruct and s["rtstruct"] != rtstruct:
                    continue
                helper = s["naming"] == "helper" or s["standard"].lower().startswith("z")
                if helper and not include_helpers and not (not glob_q and N.key(q) in (N.key(s["standard"]),
                                                                                      N.key(s["name"]))):
                    continue
                if glob_q and s["naming"] == "derived":
                    continue            # patterns match original structures only; derived ones by exact name
                if glob_q:
                    hit = fnmatch.fnmatch(s["standard"].lower(), q.lower()) or \
                          fnmatch.fnmatch(s["name"].lower(), q.lower())
                else:
                    hit = N.key(q) in (N.key(s["standard"]), N.key(s["name"]))
                    if not hit and s["naming"] != "derived" and base:
                        hit = s["standard"] == base or (s["standard"].startswith(base + "_")
                                                        and s["standard"][len(base) + 1:] in ("L", "R"))
                if hit and s not in out:
                    out.append(s)
        return out

    def image(self, series_id):
        for i in self.images:
            if i["id"] == series_id:
                return i
        return None

    def mask(self, s):
        return read_mask(s["file"])


# ------------------------------------------------------------------ basics
def read_mask(x):
    img = x if isinstance(x, sitk.Image) else sitk.ReadImage(x.get("file") if isinstance(x, dict) else str(x))
    return sitk.Cast(img > 0, sitk.sitkUInt8) if img.GetPixelID() != sitk.sitkUInt8 or \
        sitk.GetArrayViewFromImage(img).max() > 1 else img


def read_image(x):
    return x if isinstance(x, sitk.Image) else sitk.ReadImage(str(x))


def same_grid(a, b, tol=1e-3):
    return a.GetSize() == b.GetSize() and np.allclose(a.GetSpacing(), b.GetSpacing(), atol=tol) \
        and np.allclose(a.GetOrigin(), b.GetOrigin(), atol=tol) \
        and np.allclose(a.GetDirection(), b.GetDirection(), atol=tol)


def to_grid(img, ref, mask=False, default=0):
    """Resample img onto the grid of ref (nearest for masks, linear otherwise)."""
    if same_grid(img, ref):
        return img
    interp = sitk.sitkNearestNeighbor if mask else sitk.sitkLinear
    return sitk.Resample(img, ref, sitk.Transform(), interp, default, img.GetPixelID())


def _arr(m):
    return sitk.GetArrayViewFromImage(m) > 0


def _from(arr, ref):
    out = sitk.GetImageFromArray(arr.astype(np.uint8))
    out.CopyInformation(ref)
    return out


def voxel_ml(img):
    sx, sy, sz = img.GetSpacing()
    return sx * sy * sz / 1000.0


def write(img, path, overwrite=False):
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(f"exists, not overwritten: {path}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sitk.WriteImage(img, path, True)
    return path


# ------------------------------------------------------------------ operations
def combine(masks, op="union"):
    """union | intersection | minus (first minus all others). Masks on other grids are resampled onto the
    first one (nearest neighbour). Returns (mask, notes)."""
    masks = [read_mask(m) for m in masks]
    if not masks:
        raise ValueError("no structures selected")
    ref, notes = masks[0], []
    acc = _arr(ref).copy()
    for i, m in enumerate(masks[1:], 2):
        if not same_grid(m, ref):
            m = to_grid(m, ref, mask=True)
            notes.append(f"structure {i} resampled onto the grid of structure 1")
        a = _arr(m)
        if op == "union":
            acc |= a
        elif op == "intersection":
            acc &= a
        elif op == "minus":
            acc &= ~a
        else:
            raise ValueError("op must be union, intersection or minus")
    return _from(acc, ref), notes


def margin(mask, mm):
    """Expand (mm > 0) or shrink (mm < 0) by a distance in mm, measured with a Euclidean distance map.
    mm may be (x, y, z) for anisotropic expansion (all >= 0). The grid is not enlarged: a structure touching
    the image border is cut there (reported by structure_stats -> touches_border)."""
    m = read_mask(mask)
    aniso = isinstance(mm, (list, tuple))
    mx = [float(v) for v in mm] if aniso else [float(mm)] * 3
    if aniso and any(v < 0 for v in mx):
        raise ValueError("anisotropic margins must all be >= 0")
    if max(abs(v) for v in mx) == 0 or not _arr(m).any():
        return m
    # Work on a finer grid around the structure: a voxel mask is a staircase whose surface lies half a voxel
    # outside the outermost voxel centres; measuring from voxel centres loses ~half a voxel (1.5 mm with 3 mm
    # slices). Fine grid = half the smallest spacing; the mask surface is recovered by linear interpolation.
    sp = np.array(m.GetSpacing())
    ls = sitk.LabelShapeStatisticsImageFilter()
    ls.Execute(m)
    bb = ls.GetBoundingBox(1)
    pad = [int(np.ceil(max(0.0, v) / s)) + 2 for v, s in zip(mx, sp)]
    lo = [max(0, bb[i] - pad[i]) for i in range(3)]
    hi = [min(m.GetSize()[i], bb[i] + bb[i + 3] + pad[i]) for i in range(3)]
    sub = sitk.RegionOfInterest(m, [h - l for l, h in zip(lo, hi)], lo)
    f = float(sp.min()) / 2
    fine_size = [int(np.ceil(n * s / f)) for n, s in zip(sub.GetSize(), sp)]
    fine_origin = sub.TransformContinuousIndexToPhysicalPoint([-0.5 + f / (2 * s) for s in sp])
    fine = sitk.Resample(sitk.Cast(sub, sitk.sitkFloat32), fine_size, sitk.Transform(), sitk.sitkLinear,
                         fine_origin, (f, f, f), sub.GetDirection(), 0.0, sitk.sitkFloat32)
    fine = sitk.Cast(fine >= 0.5, sitk.sitkUInt8)
    if aniso:
        # distance in a space scaled by 1/margin per axis (axes with 0 margin: no growth)
        scaled = sitk.Image(fine)
        scaled.SetSpacing([f / v if v > 0 else 1e6 for v in mx])
        d = sitk.SignedMaurerDistanceMap(scaled, insideIsPositive=False, squaredDistance=False,
                                         useImageSpacing=True)
        grown = d <= 1.0 + 0.5 * f / min(v for v in mx if v > 0)
    else:
        d = sitk.SignedMaurerDistanceMap(fine, insideIsPositive=False, squaredDistance=False,
                                         useImageSpacing=True)
        # distances are measured to the outermost object voxel centres; the surface lies f/2 further out, so
        # both growing (d > 0 outside) and shrinking (d < 0 inside) shift the threshold by +f/2
        grown = d <= mx[0] + 0.5 * f
    grown = sitk.Cast(grown, sitk.sitkFloat32)
    grown.CopyInformation(fine)
    back = sitk.Resample(grown, sub, sitk.Transform(), sitk.sitkLinear, 0.0, sitk.sitkFloat32)
    out = np.zeros(sitk.GetArrayViewFromImage(m).shape, np.uint8)
    out[lo[2]:hi[2], lo[1]:hi[1], lo[0]:hi[0]] = sitk.GetArrayViewFromImage(back) >= 0.5
    return _from(out, m)


def structure_stats(mask):
    m = read_mask(mask)
    a = _arr(m)
    n = int(a.sum())
    res = {"voxels": n, "volume_ml": round(n * voxel_ml(m), 3)}
    if n == 0:
        res["empty"] = True
        return res
    cc = sitk.ConnectedComponent(m)
    ls = sitk.LabelShapeStatisticsImageFilter()
    ls.Execute(cc)
    sizes = sorted((ls.GetNumberOfPixels(lab) for lab in ls.GetLabels()), reverse=True)
    whole = sitk.LabelShapeStatisticsImageFilter()
    whole.Execute(m)
    bb = whole.GetBoundingBox(1)          # x, y, z start, then sizes (index space)
    lo = m.TransformIndexToPhysicalPoint(bb[:3])
    hi = m.TransformIndexToPhysicalPoint([bb[i] + bb[i + 3] - 1 for i in range(3)])
    res.update({
        "centroid_mm": [round(v, 1) for v in whole.GetCentroid(1)],
        "extent_mm": [round(abs(h - l) + sp, 1) for l, h, sp in zip(lo, hi, m.GetSpacing())],
        "slices": [bb[2], bb[2] + bb[5] - 1],
        "components": len(sizes),
        "largest_component_share": round(sizes[0] / n, 3),
        "touches_border": bool(a[0].any() or a[-1].any() or a[:, 0].any() or a[:, -1].any()
                               or a[:, :, 0].any() or a[:, :, -1].any()),
    })
    return res


def _surface_distances(a, b):
    """Distances (mm) from the surface of a to the surface of b."""
    sa = sitk.LabelContour(a, fullyConnected=False)
    db = sitk.Abs(sitk.SignedMaurerDistanceMap(b, insideIsPositive=False, squaredDistance=False,
                                               useImageSpacing=True))
    return sitk.GetArrayViewFromImage(db)[sitk.GetArrayViewFromImage(sa) > 0]


def compare(a, b):
    """Dice, Jaccard, volumes, Hausdorff (max, 95 %), mean surface distance. b is resampled onto a's grid."""
    a = read_mask(a)
    b = read_mask(b)
    note = None
    if not same_grid(a, b):
        b, note = to_grid(b, a, mask=True), "second structure resampled onto the grid of the first"
    x, y = _arr(a), _arr(b)
    inter, va, vb = int((x & y).sum()), int(x.sum()), int(y.sum())
    res = {"volume_a_ml": round(va * voxel_ml(a), 3), "volume_b_ml": round(vb * voxel_ml(a), 3),
           "dice": round(2 * inter / (va + vb), 4) if va + vb else None,
           "jaccard": round(inter / int((x | y).sum()), 4) if (x | y).any() else None}
    if va and vb:
        d = np.concatenate([_surface_distances(a, b), _surface_distances(b, a)])
        res.update({"hausdorff_mm": round(float(d.max()), 2), "hd95_mm": round(float(np.percentile(d, 95)), 2),
                    "mean_surface_distance_mm": round(float(d.mean()), 2)})
    if note:
        res["note"] = note
    return res


def _values_in(image, mask):
    m = read_mask(mask)
    img = read_image(image)
    note = None
    if not same_grid(img, m):
        img, note = to_grid(sitk.Cast(img, sitk.sitkFloat32), m), "image resampled (linear) onto the mask grid"
    v = sitk.GetArrayViewFromImage(img)[_arr(m)].astype(float)
    return v, m, note


def image_stats(image, mask):
    """Statistics of an image (e.g. CT in HU) inside a mask."""
    v, m, note = _values_in(image, mask)
    if v.size == 0:
        return {"empty_mask": True}
    p = np.percentile(v, [5, 50, 95])
    res = {"voxels": int(v.size), "mean": round(float(v.mean()), 2), "std": round(float(v.std()), 2),
           "min": round(float(v.min()), 2), "p5": round(float(p[0]), 2), "median": round(float(p[1]), 2),
           "p95": round(float(p[2]), 2), "max": round(float(v.max()), 2)}
    if note:
        res["note"] = note
    return res


def dose_stats(dose, mask, d_levels=(98, 95, 50, 2), v_levels=()):
    """Dose metrics in Gy: Dmean/Dmin/Dmax, Dx% (dose received by at least x % of the volume), Vx Gy (% volume
    receiving at least x Gy). Dose grid is resampled linearly onto the structure grid."""
    v, m, note = _values_in(dose, mask)
    if v.size == 0:
        return {"empty_mask": True}
    res = {"volume_ml": round(v.size * voxel_ml(m), 3), "Dmean_Gy": round(float(v.mean()), 3),
           "Dmin_Gy": round(float(v.min()), 3), "Dmax_Gy": round(float(v.max()), 3)}
    for x in d_levels:
        res[f"D{x}%_Gy"] = round(float(np.percentile(v, 100 - x)), 3)
    for g in v_levels:
        res[f"V{g}Gy_%"] = round(float((v >= g).mean() * 100), 2)
    if note:
        res["note"] = note
    return res


def dvh(dose, mask, step_gy=0.1):
    """Cumulative DVH: list of (dose_Gy, volume_%)."""
    v, _, _ = _values_in(dose, mask)
    if v.size == 0:
        return []
    edges = np.arange(0, v.max() + step_gy, step_gy)
    return [(round(float(e), 3), round(float((v >= e).mean() * 100), 3)) for e in edges]


def labelmap(items):
    """items: [(label_name, mask) or (label_name, mask, value), ...] in priority order (later items overwrite
    earlier ones where they overlap). Without value: 1, 2, 3 ... (skipping values given explicitly).
    Returns (label image UInt8, table {value: name}, overlaps {name: voxels overwritten})."""
    if not items:
        raise ValueError("no structures")
    given = {it[2] for it in items if len(it) > 2 and it[2] is not None}
    if any(not 0 < v < 256 for v in given):
        raise ValueError("label values must be 1..255")
    free = (v for v in range(1, 256) if v not in given)
    ref = read_mask(items[0][1])
    lab = np.zeros(sitk.GetArrayViewFromImage(ref).shape, np.uint8)
    table, overlaps = {0: "background"}, {}
    for it in items:
        name, m = it[0], it[1]
        value = it[2] if len(it) > 2 and it[2] is not None else next(free)
        m = to_grid(read_mask(m), ref, mask=True)
        a = _arr(m)
        over = int((a & (lab > 0) & (lab != value)).sum())
        if over:
            overlaps[name] = over
        lab[a] = value
        table[value] = name if value not in table else f"{table[value]}+{name}"
    out = sitk.GetImageFromArray(lab)
    out.CopyInformation(ref)
    return out, table, overlaps


def resample(img, spacing=None, reference=None, mask=False):
    """To a new spacing (mm, e.g. (1, 1, 1)) keeping the physical extent, or onto the grid of reference."""
    img = read_image(img)
    if reference is not None:
        return to_grid(img, read_image(reference), mask=mask, default=0 if mask else _background(img))
    sp_old, size_old = np.array(img.GetSpacing()), np.array(img.GetSize())
    sp_new = np.array(spacing, float)
    size_new = [max(1, int(round(s))) for s in size_old * sp_old / sp_new]
    interp = sitk.sitkNearestNeighbor if mask else sitk.sitkLinear
    return sitk.Resample(img, size_new, sitk.Transform(), interp, img.GetOrigin(), tuple(sp_new),
                         img.GetDirection(), 0 if mask else _background(img), img.GetPixelID())


def _background(img):
    a = sitk.GetArrayViewFromImage(img)
    return float(a.min()) if a.size else 0.0      # e.g. -1000 HU for CT


def crop(img, mask, margin_mm=10.0):
    """Crop img to the bounding box of mask plus a margin (mm). mask must share the physical space."""
    img = read_image(img)
    m = to_grid(read_mask(mask), img, mask=True)
    ls = sitk.LabelShapeStatisticsImageFilter()
    ls.Execute(m)
    if 1 not in ls.GetLabels():
        raise ValueError("mask is empty")
    bb = ls.GetBoundingBox(1)
    pad = [int(np.ceil(margin_mm / sp)) for sp in img.GetSpacing()]
    lo = [max(0, bb[i] - pad[i]) for i in range(3)]
    hi = [min(img.GetSize()[i], bb[i] + bb[i + 3] + pad[i]) for i in range(3)]
    return sitk.RegionOfInterest(img, [h - l for l, h in zip(lo, hi)], lo)


# ------------------------------------------------------------------ RTSTRUCT output (plastimatch)
def masks_to_rtstruct(masks, ref_ct_dir, out_dir, run_plastimatch):
    """masks: {name: mask image}; ref_ct_dir: DICOM folder of the reference CT (one series).
    Writes a new RTSTRUCT (one ROI per mask) into out_dir with plastimatch. Returns the RTSTRUCT path.
    The RTSTRUCT takes patient/study identifiers from the reference CT (needed to import it into a TPS)."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ss_")
    try:
        for name, m in masks.items():
            safe = re.sub(r"[^A-Za-z0-9_^-]+", "_", name)[:60] or "structure"
            sitk.WriteImage(read_mask(m), os.path.join(tmp, safe + ".mha"))
        os.makedirs(out_dir, exist_ok=True)
        run_plastimatch(["convert", "--input-prefix", tmp, "--referenced-ct", ref_ct_dir,
                         "--output-dicom", out_dir])
        found = [f for f in glob.glob(os.path.join(out_dir, "**", "*"), recursive=True) if os.path.isfile(f)]
        import pydicom
        for f in found:
            try:
                if pydicom.dcmread(f, stop_before_pixels=True).Modality == "RTSTRUCT":
                    return f
            except Exception:
                pass
        raise RuntimeError("plastimatch wrote no RTSTRUCT")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
