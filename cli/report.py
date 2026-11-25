#!/usr/bin/env python3
"""
report.py - structure report of a messy DICOM folder: patients, studies, series, RT objects (HTML + CSV + JSON).
Read-only; names, IDs and dates are masked unless --show-phi.

  python cli/report.py INPUT_DIR [-o OUTPUT_DIR] [--llm] [--show-phi]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # repo root
from toolsets.dicom import scan  # noqa: E402

if __name__ == "__main__":
    scan.main()
