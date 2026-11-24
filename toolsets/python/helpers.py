# Helpers available inside the run_python sandbox (loaded by the preamble; no imports needed in user code).
# Kept small and well tested so the model does not have to get SimpleITK details right every time.
import glob as _glob
import json as _json
import os as _os

import numpy as np
import SimpleITK as sitk


def _load_structures(mha_root):
    out = []
    for js in sorted(_glob.glob(_os.path.join(mha_root, "**", "structures.json"), recursive=True)):
        try:
            d = _json.load(open(js, encoding="utf-8"))
        except Exception:
            continue
        rt = d.get("rtstruct", "")
        for s in d.get("structures", []):
            out.append({"patient": rt.split("-")[0], "rtstruct": rt, "name": s.get("name", ""),
                        "standard": s.get("standard", s.get("name", "")), "naming": s.get("naming", ""),
                        "file": _os.path.join(_os.path.dirname(js), s.get("file", ""))})
    return out


STRUCTURES = _load_structures("/data/mha") if _os.path.isdir("/data/mha") else []


def _as_image(x):
    if isinstance(x, sitk.Image):
        return x
    if isinstance(x, dict):
        x = x.get("file", "")
    return sitk.ReadImage(str(x))


def _label(x):
    if isinstance(x, dict):
        return x.get("name", "?")
    return _os.path.basename(x) if isinstance(x, str) else "image"


def _grids():
    """(size, spacing, origin) of every exported image/mask - write_image only accepts these grids."""
    out = []
    for f in _glob.glob("/data/mha/**/*.mha", recursive=True):
        try:
            r = sitk.ImageFileReader()
            r.SetFileName(f)
            r.ReadImageInformation()
            out.append((r.GetSize(), r.GetSpacing(), r.GetOrigin()))
        except Exception:
            pass
    return out


def read_image(path):
    """Read an image or mask (.mha/.nii/.nrrd) -> SimpleITK image."""
    return sitk.ReadImage(path)


def combine_masks(paths, op="union"):
    """Combine binary masks on the same grid. op: 'union' | 'intersection' | 'minus' (first minus the rest).
    Returns a SimpleITK UInt8 image (0/1) with the geometry of the first mask."""
    paths = [paths] if isinstance(paths, (str, dict, sitk.Image)) else list(paths)
    if not paths:
        raise ValueError("no masks given")
    imgs = [_as_image(p) for p in paths]          # paths, STRUCTURES entries or SimpleITK images
    first = imgs[0]
    acc = sitk.GetArrayFromImage(first) > 0
    for p, img in zip(paths[1:], imgs[1:]):
        if img.GetSize() != first.GetSize() or not np.allclose(img.GetSpacing(), first.GetSpacing()) \
                or not np.allclose(img.GetOrigin(), first.GetOrigin()):
            raise ValueError(f"{_label(p)} is on a different grid than {_label(paths[0])}")
        a = sitk.GetArrayFromImage(img) > 0
        acc = acc | a if op == "union" else acc & a if op == "intersection" else acc & ~a
    out = sitk.GetImageFromArray(acc.astype(np.uint8))
    out.CopyInformation(first)
    return out


def mask_volume_ml(mask):
    """Volume of a mask (SimpleITK image or path) in ml."""
    m = sitk.ReadImage(mask) if isinstance(mask, str) else mask
    sx, sy, sz = m.GetSpacing()
    return float((sitk.GetArrayFromImage(m) > 0).sum()) * sx * sy * sz / 1000.0


def write_image(img, filename, check=True):
    """Write a SimpleITK image to WORK/<filename> (compressed). Returns the path.
    check=True: refuse images whose grid (size/spacing/origin) matches no exported CT or mask - such an image
    has lost its geometry (e.g. built from a numpy array without CopyInformation) and would not line up."""
    if not isinstance(img, sitk.Image):
        raise TypeError("write_image needs a SimpleITK image (e.g. from combine_masks), not " + type(img).__name__)
    grids = _grids() if check else []
    if grids and not any(img.GetDimension() == len(s) and tuple(img.GetSize()) == tuple(s)
                         and np.allclose(img.GetSpacing(), sp) and np.allclose(img.GetOrigin(), o)
                         for s, sp, o in grids):
        raise ValueError(f"NOT WRITTEN: image grid size={img.GetSize()} spacing={img.GetSpacing()} "
                         f"origin={img.GetOrigin()} matches no exported CT/mask - geometry lost. Use combine_masks(), "
                         "or img.CopyInformation(read_image(reference_mask)) after GetImageFromArray.")
    path = _os.path.join("/data/work", _os.path.basename(filename))
    sitk.WriteImage(img, path, True)
    return path
