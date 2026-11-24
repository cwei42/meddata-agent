#!/usr/bin/env python3
"""
dicom_organize.py - copy a messy DICOM folder into Patient/Study/Series folders (no LLM needed).

  python dicom_organize.py INPUT [-o OUTPUT] [--yes] [--show-phi]

Shows the plan first and asks before copying. Originals are never changed.
Default output: <INPUT>_organized next to the input.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from toolsets.dicom import layout as L     # noqa: E402
from toolsets.dicom import organize as O  # noqa: E402
from toolsets.dicom import scan as S      # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Organize a DICOM folder (copy, never move).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--yes", action="store_true", help="copy without asking")
    ap.add_argument("--show-phi", action="store_true", help="real source paths in manifest.csv")
    args = ap.parse_args()

    root = L.input_dir(args.input)
    L.use_session_tmp(root)
    out = os.path.abspath(os.path.expanduser(args.output or L.out(root, "organized")))
    err = O.check_output(root, out)
    if err:
        sys.exit(err)
    print(f"Scanning {root} ...", file=sys.stderr)
    res = S.scan(root)
    tree, rows, files = S.build_tree(res["instances"], show_phi=False)
    tokens = set()
    for i in res["instances"]:
        tokens |= S.name_tokens(i["patient_name"])
    plan = O.make_plan(root, res, tree, rows, files, show_phi=args.show_phi)
    print(O.describe_plan(plan, out, max_lines=60))
    print(f"\n(not DICOM / skipped: {res['non_dicom']} files, DICOMDIR: {len(res['dicomdirs'])})")
    if not args.yes:
        if input("\nCopy now? [y/N] ").strip().lower() not in ("y", "yes", "j", "ja"):
            sys.exit("nothing copied")
    r = O.execute(plan, out, mask=lambda t: S.mask_name(t, tokens)[0])
    print(f"\ndone: {r['copied']} copied, {r['duplicates']} duplicates skipped, "
          f"{len(r['failed'])} failed verification -> {r['out']}")
    if r["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
