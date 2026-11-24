#!/usr/bin/env python3
"""
dicom_structures.py - list RTSTRUCT structure names and their TG-263 names (nothing is changed).

  python dicom_structures.py INPUT [--map FILE.csv] [--unmapped OUT.csv] [--all]

  --map       mapping file original,standard (default configs/structure_names.csv; its rows win)
  --unmapped  write names that could not be mapped to this CSV, to fill in and add to the mapping file
  --all       also show names that are already TG-263
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolsets.dicom import structnames as N               # noqa: E402
from toolsets.dicom.common import Scanned                 # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="RTSTRUCT structure names -> TG-263 (report only).")
    ap.add_argument("input")
    ap.add_argument("--map", help="mapping CSV (original,standard)")
    ap.add_argument("--unmapped", help="write unmapped names to this CSV")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    mapping, problems = N.load_mapping(args.map)
    for p in problems:
        print("mapping file:", p, file=sys.stderr)
    sc = Scanned(args.input)
    counts, unmapped = {}, {}
    for r in sc.rows:
        if r["modality"] != "RTSTRUCT":
            continue
        for path in sc.files[r["id"]]:
            try:
                names = N.rtstruct_roi_names(os.path.join(sc.root, path))
            except Exception as e:
                print(f"{r['id']}: cannot read ({e})")
                continue
            print(f"\n{r['id']}  {sc.redact(r['series_description'])}  ({len(names)} structures)")
            for x in N.standardize_all(names, mapping):
                counts[x["status"]] = counts.get(x["status"], 0) + 1
                if x["status"] == "unmapped":
                    unmapped.setdefault(N.key(x["original"]), sc.redact(x["original"]))
                if x["status"] == "standard" and not args.all:
                    continue
                print(f"  {sc.redact(x['original'])[:28]:28} -> {sc.redact(x['standard']):22} "
                      f"{x['status']:9} {sc.redact(x['note'])}")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) if counts else "\nno RTSTRUCT found")
    if args.unmapped and unmapped:
        N.write_template([{"original": v} for v in unmapped.values()], args.unmapped)
        print(f"unmapped names written to {args.unmapped} - fill in 'standard', add the rows to "
              f"{args.map or N.DEFAULT_MAPPING}")


if __name__ == "__main__":
    main()
