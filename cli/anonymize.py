#!/usr/bin/env python3
"""
anonymize.py - anonymized DICOM copy in a clean Patient/Study/Series structure.

  python cli/anonymize.py INPUT [-o OUTPUT] [--seed SECRET] [--keep-private] [--write-mapping] [--yes]

Uses dicognito (names, IDs, dates shifted consistently, UIDs remapped) plus extra rules
(free-text masking, device/physician fields, private tags). Every output file is re-checked.
Pixel data is not changed - see anonymization_report.txt for series to review.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root
from toolsets.dicom import anonymize as A                 # noqa: E402
from toolsets.dicom import organize as O                  # noqa: E402
from toolsets.dicom.common import Scanned, confirm        # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Anonymize a DICOM folder (copy).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--seed", help="secret seed for consistent pseudonyms across runs (keep it secret)")
    ap.add_argument("--keep-private", action="store_true", help="keep private vendor tags")
    ap.add_argument("--write-mapping", action="store_true", help="write original->new PatientID table")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    sc = Scanned(args.input)
    out = os.path.abspath(os.path.expanduser(args.output or sc.default_out("anonymized")))
    err = O.check_output(sc.root, out)
    if err:
        sys.exit(err)
    plan = O.make_plan(sc.root, sc.res, sc.tree, sc.rows, sc.files, include_year=False)
    print(O.describe_plan(plan, out, max_lines=40))
    print("\nAnonymization: dicognito + free-text masking + extra fields"
          f"{'' if args.keep_private else ' + private tags removed'}; seed: "
          f"{'given' if args.seed else 'random (irreversible)'}; mapping: {'yes' if args.write_mapping else 'no'}")
    if not confirm("Write anonymized copy now?", args.yes):
        sys.exit("nothing written")
    r = A.run(sc.root, out, sc.res, sc.tree, sc.rows, sc.files, seed=args.seed,
              keep_private=args.keep_private, write_mapping=args.write_mapping)
    print(f"\nwritten: {r['written']} files in {r['series']} series -> {r['out']}")
    print(f"free-text fields masked: {r['masked']}")
    print(f"verification problems: {len(r['problems'])}")
    for p in r["problems"][:20]:
        print("  !", p)
    print(f"failed: {len(r['failed'])}")
    if r["risky_series"]:
        print(f"series to review for burned-in text: {len(r['risky_series'])} (see anonymization_report.txt)")
    sys.exit(1 if r["problems"] or r["failed"] else 0)


if __name__ == "__main__":
    main()
