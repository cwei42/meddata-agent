"""Shared helpers for the DICOM command-line tools."""
import os
import sys

from . import layout as L
from . import scan as S


class Scanned:
    """Scan a folder once; keep everything the tools need."""

    def __init__(self, folder):
        self.root = L.input_dir(folder)          # case folder or its original/ -> original/
        L.use_session_tmp(self.root)             # temp files stay inside the encrypted space
        if not os.path.isdir(self.root):
            sys.exit(f"not a folder: {self.root}")
        print(f"Scanning {self.root} ...", file=sys.stderr)
        self.res = S.scan(self.root)
        self.tree, self.rows, self.files = S.build_tree(self.res["instances"], show_phi=False)
        self.tokens = set()
        for i in self.res["instances"]:
            self.tokens |= S.name_tokens(i["patient_name"])
        print(f"  {len(self.tree)} patients, {sum(len(p['studies']) for p in self.tree)} studies, "
              f"{len(self.rows)} series", file=sys.stderr)

    def redact(self, text):
        return S.mask_name(text, self.tokens)[0] if self.tokens else text

    def default_out(self, step):
        """Output folder of a processing step (see layout.py), e.g. 'mha', 'anonymized'."""
        return L.out(self.root, step)


def confirm(question, yes=False):
    if yes:
        return True
    try:
        return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes", "j", "ja")
    except EOFError:
        return False
