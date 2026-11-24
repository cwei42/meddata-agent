"""Anonymize a DICOM folder into a clean Patient/Study/Series structure.

Core anonymization is done by dicognito (https://github.com/blairconrad/dicognito):
names, IDs, addresses, institutions replaced; all dates shifted by one consistent
offset (intervals preserved); all UIDs remapped consistently (series/study links preserved).

Extra rules added here (gaps found in testing):
  * free-text fields (descriptions, comments, protocol) are masked for parts of the
    original patient name/ID
  * device serial number, physician/operator names, other patient IDs etc. are emptied
  * private (vendor) tags are removed unless keep_private=True
  * PatientName becomes "ANON^<new PatientID>" (obviously not a real person)

Pixel data is NOT changed. Series that may contain burned-in text (ultrasound,
screenshots, secondary capture, BurnedInAnnotation=YES) are listed for manual review.

Every output file is re-read and checked afterwards (verify()).
"""
import csv
import datetime as dt
import os
import re
import secrets
import shutil

import pydicom
from dicognito.anonymizer import Anonymizer
try:
    from importlib.metadata import version as _v
    _DICOGNITO_VERSION = _v("dicognito")
except Exception:
    _DICOGNITO_VERSION = "?"

from . import layout as L
from . import organize as O
from . import scan as S

TEXT_VRS = {"LO", "SH", "LT", "ST", "UT", "PN", "UC"}
EXTRA_EMPTY = [
    "DeviceSerialNumber", "OtherPatientIDs", "OtherPatientNames", "OtherPatientIDsSequence",
    "PatientTelephoneNumbers", "PatientMotherBirthName", "MilitaryRank", "BranchOfService",
    "MedicalRecordLocator", "ReferringPhysicianTelephoneNumbers", "PerformingPhysicianName",
    "NameOfPhysiciansReadingStudy", "OperatorsName", "PhysiciansOfRecord", "RequestingPhysician",
    "ScheduledPerformingPhysicianName", "ReferringPhysicianName", "AdditionalPatientHistory",
    "PatientComments", "RequestAttributesSequence", "InstitutionalDepartmentName", "StationName",
]
RISKY_MODALITIES = {"US", "OT", "XC", "ES", "SM", "DOC", "SC"}
RISKY_SOP = {"1.2.840.10008.5.1.4.1.1.7", "1.2.840.10008.5.1.4.1.1.7.1", "1.2.840.10008.5.1.4.1.1.7.2",
             "1.2.840.10008.5.1.4.1.1.7.3", "1.2.840.10008.5.1.4.1.1.7.4", "1.2.840.10008.5.1.4.1.1.104.1"}


def _date_forms(d):
    d = re.sub(r"\D", "", str(d or ""))
    if len(d) != 8:
        return set()
    y, m, dd = d[:4], d[4:6], d[6:]
    return {d, f"{y}-{m}-{dd}", f"{dd}.{m}.{y}", f"{y}/{m}/{dd}", f"{dd}/{m}/{y}"}


def _identifiers(ds):
    """Strings that must not survive anywhere in free text: parts of the patient's name AND of every other
    person named in the file (physicians, operators, ROI interpreter ...), patient ID / other IDs / accession
    number, the birth date in all common spellings (19650312, 1965-03-12, 12.03.1965 ...), institution name."""
    toks = set(S.name_tokens(S.s(ds, "PatientName")))
    for el in ds.iterall():                        # every person name in the file, also inside sequences
        if el.VR == "PN" and el.value:
            vals = el.value if isinstance(el.value, (list, pydicom.multival.MultiValue)) else [el.value]
            for v in vals:
                toks |= S.name_tokens(str(v))
    for kw in ("PatientID", "AccessionNumber", "OtherPatientIDs"):
        v = S.s(ds, kw).strip()
        if len(v) >= 4:
            toks.add(v.lower())
    toks |= _date_forms(S.s(ds, "PatientBirthDate"))
    toks |= {t for t in S.name_tokens(S.s(ds, "InstitutionName")) if len(t) >= 5}
    return toks


def _mask_free_text(ds, toks):
    n = 0
    for el in ds.iterall():
        if el.VR in TEXT_VRS and el.VR != "PN" and isinstance(el.value, str) and el.value:
            new, hit = S.mask_name(el.value, toks)
            if hit:
                el.value = new.replace("[NAME]", "XXX")[:64 if el.VR in ("LO", "PN") else 16 if el.VR == "SH" else 10240]
                n += 1
    return n


def _walk(ds):
    """The dataset and every dataset nested in sequences."""
    yield ds
    for el in list(ds):
        if el.VR == "SQ" and el.value:
            for item in el.value:
                yield from _walk(item)


def read_any(path):
    """Read a DICOM file, also raw files without preamble/file meta; give them valid file meta."""
    with open(path, "rb") as f:
        pre = f.read(132)
    has_meta = len(pre) == 132 and pre[128:132] == b"DICM"
    ds = pydicom.dcmread(path, force=not has_meta)
    if not has_meta or "TransferSyntaxUID" not in getattr(ds, "file_meta", {}):
        from pydicom.dataset import FileMetaDataset
        from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
        implicit = True
        enc = getattr(ds, "original_encoding", None)
        if enc and enc[0] is not None:
            implicit = enc[0]
        fm = getattr(ds, "file_meta", None) or FileMetaDataset()
        fm.TransferSyntaxUID = ImplicitVRLittleEndian if implicit else ExplicitVRLittleEndian
        fm.MediaStorageSOPClassUID = S.s(ds, "SOPClassUID")
        fm.MediaStorageSOPInstanceUID = S.s(ds, "SOPInstanceUID")
        ds.file_meta = fm
    return ds


def anonymize_dataset(ds, anonymizer, keep_private=False):
    toks = _identifiers(ds)
    risky = (S.s(ds, "BurnedInAnnotation").upper() == "YES" or S.s(ds, "Modality") in RISKY_MODALITIES
             or S.s(ds, "SOPClassUID") in RISKY_SOP)
    anonymizer.anonymize(ds)
    for sub in _walk(ds):                      # also inside sequences (e.g. RT plan beam sequence)
        for kw in EXTRA_EMPTY:
            if kw in sub:
                el = sub[kw]
                if el.VR == "SQ":
                    del sub[kw]
                else:
                    el.value = ""
    if not keep_private:
        ds.remove_private_tags()
    masked = _mask_free_text(ds, toks)
    ds.PatientName = f"ANON^{S.s(ds, 'PatientID')}"
    ds.PatientIdentityRemoved = "YES"
    ds.DeidentificationMethod = (f"dicognito {_DICOGNITO_VERSION} + orin-agent {L.version()}"
                                 + ("" if not keep_private else " (private kept)"))[:64]
    return toks, risky, masked


def verify(out, forbidden_by_file):
    """Re-read every output file; check it against ITS OWN patient's original identifiers,
    for private tags and for a non-ANON PatientName. Reports element names, never values."""
    problems, n = [], 0
    for dirpath, _, files in os.walk(out):
        for fn in files:
            if not fn.endswith(".dcm"):
                continue
            n += 1
            p = os.path.join(dirpath, fn)
            ds = pydicom.dcmread(p, stop_before_pixels=True)
            rel = os.path.relpath(p, out)
            forbidden = forbidden_by_file.get(rel, set())
            if not S.s(ds, "PatientName").startswith("ANON^"):
                problems.append(f"{rel}: PatientName not anonymized")
            for el in ds.iterall():
                if el.tag.is_private:
                    problems.append(f"{rel}: private tag {el.tag}")
                    break
            pats = [re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])") for t in forbidden if len(t) >= 3]
            for el in ds.iterall():
                if el.VR in TEXT_VRS | {"DA", "DT"} and el.value not in (None, ""):
                    v = str(el.value).lower()
                    if any(p.search(v) for p in pats):
                        problems.append(f"{rel}: original identifier in {el.keyword or el.tag}")
                        break
    return n, problems


def run(root, out, res, tree, rows, files, seed=None, keep_private=False, write_mapping=False):
    err = O.check_output(root, out)
    if err:
        raise ValueError(err)
    plan = O.make_plan(root, res, tree, rows, files, include_year=False)
    parent = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(parent, exist_ok=True)
    if shutil.disk_usage(parent).free < plan["total_bytes"] * 1.1 + 10 * 2**20:
        raise ValueError("not enough free space")
    secret_seed = seed or secrets.token_hex(16)        # random + not stored => irreversible
    anonymizer = Anonymizer(seed=secret_seed)
    os.makedirs(out)
    forbidden, risky_series, mapping, failed = {}, set(), {}, []
    masked_total = 0
    for src_rel, dst_rel, _size in plan["items"]:
        dst = os.path.join(out, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            raise ValueError(f"refusing to overwrite {dst_rel}")
        try:
            ds = read_any(os.path.join(root, src_rel))
            old_id = S.s(ds, "PatientID")
            toks, risky, masked = anonymize_dataset(ds, anonymizer, keep_private)
            forbidden[dst_rel] = toks
            masked_total += masked
            if risky:
                risky_series.add(os.path.dirname(dst_rel))
            mapping.setdefault(old_id, S.s(ds, "PatientID"))
            ds.save_as(dst, enforce_file_format=True)
        except Exception as e:
            failed.append(f"{dst_rel}: {type(e).__name__}: {e}")
            if os.path.exists(dst):
                os.remove(dst)
    n_checked, problems = verify(out, forbidden)
    if write_mapping:
        with open(os.path.join(out, "MAPPING_contains_original_ids.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["original_PatientID", "anonymized_PatientID"])
            w.writerows(sorted(mapping.items()))
    report = [
        f"Anonymized {dt.datetime.now():%Y-%m-%d %H:%M} with dicognito + orin-agent rules",
        f"source: {root}",
        f"files written: {len(plan['items']) - len(failed)} / {len(plan['items'])}  "
        f"(duplicates skipped: {len(plan['duplicates'])})",
        f"free-text fields masked: {masked_total}",
        f"private tags: {'kept' if keep_private else 'removed'}",
        f"seed: {'given by user (keep it secret - it allows consistent re-runs)' if seed else 'random, not stored (irreversible)'}",
        f"mapping file: {'WRITTEN - contains original IDs, protect it' if write_mapping else 'not written'}",
        "",
        f"VERIFICATION: {n_checked} files re-read, {len(problems)} problems",
        *[f"  ! {p}" for p in problems[:200]],
        "",
        f"FAILED files: {len(failed)}", *[f"  ! {x}" for x in failed[:100]],
        "",
        "PIXEL DATA NOT CHANGED. Review these series for burned-in text (names on images):",
        *([f"  - {s}" for s in sorted(risky_series)] or ["  (none)"]),
    ]
    report.insert(0, f"tool: orin-agent {L.version()}, dicognito {_DICOGNITO_VERSION}")
    with open(os.path.join(out, "anonymization_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    return {"written": len(plan["items"]) - len(failed), "failed": failed, "problems": problems,
            "risky_series": sorted(risky_series), "masked": masked_total, "out": out,
            "series": len(plan["series"])}
