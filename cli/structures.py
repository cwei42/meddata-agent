#!/usr/bin/env python3
"""
structures.py - work with structures (RTSTRUCT masks) and MHA images of a DICOM dataset, no model needed.
Needs the MHA export first (cli/to_mha.py). Results go to <case>/structures/<patient>/ (never overwritten).

  python cli/structures.py DATASET list
  python cli/structures.py DATASET stats   "GTV*"
  python cli/structures.py DATASET compare "GTV" "GTVn 1"                # Dice, Jaccard, HD95, mean surface dist.
  python cli/structures.py DATASET values  "PTV" [--image P001-S01-04] [--v 20,30]   # HU or dose (D95, V20...)
  python cli/structures.py DATASET dvh     "PTV,Heart" --dose P001-S01-04
  python cli/structures.py DATASET merge   "GTV*" --name GTV_all [--op union|intersection|minus] [--margin 5]
  python cli/structures.py DATASET margin  "GTV" --mm 5 [--name GTV+5mm]       # --mm -3 shrinks, --mm 5,5,10 x,y,z
  python cli/structures.py DATASET labelmap "GTV=GTV*" "PTV=PTV" [--name labels]   # later label wins on overlap
  python cli/structures.py DATASET crop    "PTV" --margin 20                   # image + all masks around PTV
  python cli/structures.py DATASET resample --spacing 1,1,1

Select structures by TG-263 name, original name or glob ("GTV*", "Parotid_?"), several with ",".
Helper structures (z..., ring, opt) are left out of globs. --patient P001 limits to one patient.
merge/margin --rtstruct also writes a new RTSTRUCT (on the reference CT, via plastimatch) for the TPS.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root
from toolsets.dicom import layout as L                    # noqa: E402
from toolsets.dicom import mha_tools as T                 # noqa: E402
from toolsets.dicom import structure_ops as OPS           # noqa: E402


def floats(s):
    v = [float(x) for x in str(s).split(",")]
    return v[0] if len(v) == 1 else v


def rtstruct_writer(dataset, out_dir):
    """Returns f(patient, masks, ref_id) that writes an RTSTRUCT on the reference CT of the dataset."""
    from toolsets.dicom.common import Scanned
    sc = Scanned(dataset)
    return OPS.rtstruct_writer(sc.root, sc.files, out_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", help="case folder (or a DICOM folder); its MHA export must exist")
    ap.add_argument("command", choices=["list", "stats", "compare", "values", "dvh", "merge", "margin",
                                        "labelmap", "crop", "resample"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--export", help="MHA export folder (default <case>/mha)")
    ap.add_argument("-o", "--out", help="output folder (default <case>/structures)")
    ap.add_argument("--patient")
    ap.add_argument("--name")
    ap.add_argument("--op", default="union", choices=["union", "intersection", "minus"])
    ap.add_argument("--margin", type=floats, default=None, help="mm (merge: added margin; crop: box margin)")
    ap.add_argument("--mm", type=floats, help="margin: mm, negative shrinks, or x,y,z")
    ap.add_argument("--image", help="series id of the image for 'values' (default: reference image)")
    ap.add_argument("--dose", help="series id of the RTDOSE for 'dvh'")
    ap.add_argument("--v", default="", help="dose levels for Vx, e.g. 20,30 (Gy)")
    ap.add_argument("--spacing", type=floats)
    ap.add_argument("--rtstruct", action="store_true", help="also write an RTSTRUCT (merge/margin)")
    a = ap.parse_args()

    ds = L.input_dir(a.dataset)
    L.use_session_tmp(ds)
    out = os.path.abspath(os.path.expanduser(a.out or L.out(ds, "structures")))
    ex = T.Export(a.export or L.out(ds, "mha"), derived=out)
    rt = rtstruct_writer(ds, out) if a.rtstruct else None
    need = {"stats": 1, "compare": 2, "values": 1, "dvh": 1, "merge": 1, "margin": 1, "crop": 1}
    if len(a.args) < need.get(a.command, 0):
        sys.exit(f"{a.command} needs {need[a.command]} structure selection(s)")
    c = a.command
    written = []
    if c == "list":
        print(OPS.list_structures(ex, a.patient))
    elif c == "stats":
        print(OPS.stats(ex, a.args[0], a.patient))
    elif c == "compare":
        print(OPS.compare(ex, a.args[0], a.args[1], a.patient))
    elif c == "values":
        v = tuple(float(x) for x in a.v.split(",") if x)
        print(OPS.values(ex, a.args[0], a.image, a.patient, v_levels=v))
    elif c == "dvh":
        if not a.dose:
            sys.exit("dvh needs --dose SERIES_ID (an RTDOSE in the export)")
        text, written = OPS.write_dvh(ex, out, a.args[0], a.dose, a.patient)
        print(text)
    elif c == "merge":
        text, written = OPS.merge(ex, out, a.args[0], a.name or "merged", a.op, a.margin or 0, a.patient, rt)
        print(text)
    elif c == "margin":
        if a.mm is None:
            sys.exit("margin needs --mm (e.g. 5, -3 or 5,5,10)")
        mm_txt = ",".join(f"{x:g}" for x in a.mm) if isinstance(a.mm, list) else f"{a.mm:+g}"
        text, written = OPS.merge(ex, out, a.args[0], a.name or f"{a.args[0].strip('*?')}{mm_txt}mm", "union",
                                  a.mm, a.patient, rt)
        print(text)
    elif c == "labelmap":
        if not a.args:
            sys.exit('labelmap needs labels, e.g. "PTV=1" "GTV*=2"  or  "GTV=GTV*" "PTV=PTV"')
        text, written = OPS.labelmap(ex, out, "; ".join(a.args), a.name or "labels", a.patient)
        print(text)
    elif c == "crop":
        text, written = OPS.crop(ex, out, a.args[0], a.margin if a.margin is not None else 20, a.patient, a.image)
        print(text)
    elif c == "resample":
        if not a.spacing:
            sys.exit("resample needs --spacing, e.g. 1,1,1")
        sp = a.spacing if isinstance(a.spacing, list) else [a.spacing] * 3
        text, written = OPS.resample_all(ex, out, sp, a.patient)
        print(text)
    if written:
        print(f"\n{len(written)} file(s) written under {out}")


if __name__ == "__main__":
    try:
        main()
    except (FileExistsError, FileNotFoundError, ValueError) as e:
        sys.exit(f"stopped: {e}")
