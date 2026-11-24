"""Folder layout - one folder per case (import), one sub-folder per processing step.

    /data/medical/<case>/original/     the imported data, never changed
    /data/medical/<case>/report/       structure report (HTML/CSV)
    /data/medical/<case>/organized/    sorted DICOM copy
    /data/medical/<case>/anonymized/   anonymized DICOM
    /data/medical/<case>/mha/          MHA images + structure masks
    /data/medical/<case>/structures/   merged / expanded structures, RTSTRUCTs, label maps, DVHs
    /data/medical/<case>/work/         results of the agent's own code

Every tool may be given the case folder or its original/ folder. Any other folder (e.g. test data in the home
folder) keeps the older convention <folder>_<step> next to it.
"""
import os

ORIGINAL = "original"
STEPS = ("report", "organized", "anonymized", "mha", "structures", "work")
LEGACY = {"report": "_dicom_report", "organized": "_organized", "anonymized": "_anonymized", "mha": "_mha",
          "structures": "_derived", "work": "_work"}


def _abs(folder):
    return os.path.abspath(os.path.expanduser(str(folder))).rstrip("/") or "/"


def case_dir(folder):
    """The case folder if `folder` is a case or its original/ folder, else None."""
    f = _abs(folder)
    if os.path.basename(f) == ORIGINAL and os.path.isdir(f):
        return os.path.dirname(f)
    if os.path.isdir(os.path.join(f, ORIGINAL)):
        return f
    return None


def input_dir(folder):
    """The folder with the DICOM data to read."""
    c = case_dir(folder)
    return os.path.join(c, ORIGINAL) if c else _abs(folder)


def out(folder, step):
    """Output folder of a processing step for this dataset."""
    if step not in STEPS:
        raise ValueError(f"unknown step {step}")
    c = case_dir(folder)
    return os.path.join(c, step) if c else input_dir(folder) + LEGACY[step]


def all_outputs(folder):
    return {s: out(folder, s) for s in STEPS}


def version():
    """Tool version written into results (VERSION file + git commit if the code is a git checkout)."""
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        v = open(os.path.join(repo, "VERSION")).read().strip()
    except OSError:
        v = "unknown"
    try:
        commit = open(os.path.join(repo, "COMMIT")).read().strip()[:10]     # written at deploy time
        v += f"+{commit}"
    except OSError:
        pass
    return v


# ------------------------------------------------------------------ temporary files
VAULT = "/data/medical"
SESSION_TMP = os.path.join(VAULT, ".tmp")


def use_session_tmp(folder=None):
    """While a session space is mounted, ALL temporary files (ours, plastimatch's, Python's tempfile) go into
    the encrypted space - never to /tmp on the unencrypted system disk, where deleted files can be recovered.
    Call this before any work on data in the vault. Returns the temp folder used."""
    import tempfile
    f = _abs(folder) if folder else VAULT
    if not (f == VAULT or f.startswith(VAULT + "/")) or not os.path.ismount(VAULT):
        return tempfile.gettempdir()
    os.makedirs(SESSION_TMP, mode=0o700, exist_ok=True)
    os.environ["TMPDIR"] = SESSION_TMP          # inherited by plastimatch and every other subprocess
    tempfile.tempdir = SESSION_TMP
    return SESSION_TMP
