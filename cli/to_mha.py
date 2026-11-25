#!/usr/bin/env python3
"""
to_mha.py - export image series and RTSTRUCTs to MHA with plastimatch.

  python cli/to_mha.py INPUT [-o OUTPUT] [--ref REF | --ref RTSTRUCT_ID=REF ...] [--only ID ...] [--yes]

REF = a series id from the scan (e.g. P001-S01-02), a DICOM folder, or an image file (.mha/.nii/.nrrd).
  --ref REF              use REF for every RTSTRUCT
  --ref P001-S01-05=REF  use REF for this RTSTRUCT only
Without --ref the referenced CT/MR is found automatically (RTReferencedSeriesSequence, then FrameOfReferenceUID).
Use --list to see series ids first.
Mask files are named by AAPM TG-263 (see cli/structure_names.py); --original-names keeps the original names.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root
from toolsets.dicom import export_mha as E                # noqa: E402
from toolsets.dicom import organize as O                  # noqa: E402
from toolsets.dicom.common import Scanned, confirm        # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="DICOM -> MHA (images + RTSTRUCT masks) via plastimatch.")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--ref", action="append", default=[], help="reference image for RTSTRUCT (see above)")
    ap.add_argument("--ref-modality", choices=["auto", "CT", "MR"], default="auto",
                    help="reference image for RTSTRUCT masks: auto = the series each RTSTRUCT names, "
                         "CT / MR = that modality from the same plan folder")
    ap.add_argument("--only", nargs="+", help="export only these series ids")
    ap.add_argument("--list", action="store_true", help="list series ids and exit")
    ap.add_argument("--names-map", help="structure name mapping CSV (default configs/structure_names.csv)")
    ap.add_argument("--original-names", action="store_true", help="keep original structure names (no TG-263)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    sc = Scanned(args.input)
    if args.list:
        for r in sc.rows:
            print(f"{r['id']:14} {r['modality']:8} {r['category']:17} n={r['n_files']:<4} {r['series_description']}")
        return
    refs = {}
    for x in args.ref:
        k, _, v = x.partition("=") if "=" in x and not os.path.exists(x) else ("*", "", x)
        refs[k] = v
    out = os.path.abspath(os.path.expanduser(args.output or sc.default_out("mha")))
    err = O.check_output(sc.root, out)
    if err:
        sys.exit(err)
    rows = [r for r in sc.rows if not args.only or r["id"] in args.only or r["id"] in refs.values()]
    print(E.describe(rows, out))
    if refs:
        print("references:", ", ".join(f"{k} -> {v}" for k, v in refs.items()))
    print("structure masks on:", "the series each RTSTRUCT names" if args.ref_modality == "auto"
          else f"the {args.ref_modality} of the same plan")
    if not E.available():
        sys.exit(f"plastimatch not found at {E.PLASTIMATCH}")
    if not confirm("Export now?", args.yes):
        sys.exit("nothing written")
    only = set(args.only) | set(v for v in refs.values() if v in {r["id"] for r in sc.rows}) if args.only else None
    r = E.run(sc.root, out, sc.res, sc.tree, sc.rows, sc.files, references=refs, redact=sc.redact, only=only,
              standard_names=not args.original_names, mapping_path=args.names_map,
              reference_modality=None if args.ref_modality == "auto" else args.ref_modality)
    print(f"\nimages: {r['images']}, RTSTRUCTs: {r['rtstructs']}, skipped: {r['skipped']}, failed: {len(r['failed'])}")
    for x in r["rt_details"]:
        print("  RT:", x)
    for x in r["failed"]:
        print("  !", x)
    print(f"report: {os.path.join(out, 'export_report.txt')}")


if __name__ == "__main__":
    main()
