"""Last check before data leaves the Orin: search the files to export for the case's original identifiers.

    ids = identifiers(case_original_dir)      # names (parts), patient IDs, birth dates, accession numbers
    r = scan(folder, ids)                     # {"files", "checked", "unchecked", "hits": [(rel, where)]}

What is searched (reports only WHERE something was found, never the value):
  DICOM files      every header element incl. nested sequences and private tags (pixel data is not text and
                   is not searched - burned-in text needs a visual check)
  .mha/.mhd/.nrrd  the text header (not the compressed voxel data)
  text files       .json .csv .txt .html .md .tsv .xml .log - full text
  anything else    counted as "unchecked"
"""
import os
import re

import pydicom

from . import scan as S

TEXT_EXT = (".json", ".csv", ".txt", ".html", ".htm", ".md", ".tsv", ".xml", ".log", ".sha256")
HEADER_EXT = (".mha", ".mhd", ".nrrd", ".nhdr")
TEXT_VRS = {"AE", "AS", "CS", "DA", "DS", "DT", "IS", "LO", "LT", "PN", "SH", "ST", "TM", "UC", "UI", "UR", "UT"}


def _date_forms(d):
    d = re.sub(r"\D", "", str(d or ""))
    if len(d) != 8:
        return set()
    y, m, dd = d[:4], d[4:6], d[6:]
    return {d, f"{y}-{m}-{dd}", f"{dd}.{m}.{y}", f"{y}/{m}/{dd}", f"{dd}/{m}/{y}"}


def identifiers(original_dir):
    """All identifiers of all patients in the case's original data (read from the DICOM headers)."""
    names, ids, dates = set(), set(), set()
    for dirpath, _, files in os.walk(original_dir):
        for fn in files:
            try:
                ds = pydicom.dcmread(os.path.join(dirpath, fn), stop_before_pixels=True, force=True)
            except Exception:
                continue
            names |= S.name_tokens(S.s(ds, "PatientName"))
            for k in ("PatientID", "AccessionNumber", "OtherPatientIDs"):
                v = S.s(ds, k).strip()
                if len(v) >= 3:
                    ids.add(v.lower())
            dates |= _date_forms(S.s(ds, "PatientBirthDate"))
    return {"names": names, "ids": ids, "dates": dates}


def _patterns(ids):
    pats = []
    for t in ids["names"]:
        e = re.escape(t)
        pats.append(re.compile(e if len(t) >= 5 else rf"(?<![a-z]){e}(?![a-z])"))
    for t in ids["ids"] | ids["dates"]:
        pats.append(re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])"))
    return pats


def _hit(text, pats):
    t = str(text).lower()
    return any(p.search(t) for p in pats)


def scan_name(name, ids):
    """True if a (folder) name contains one of the identifiers."""
    pats = _patterns(ids)
    return bool(pats) and _hit(name, pats)


def scan(folder, ids, max_text_mb=50):
    pats = _patterns(ids)
    res = {"files": 0, "checked": 0, "unchecked": [], "hits": []}
    if not pats:
        res["note"] = "no identifiers found in the original data - nothing to search for"
    for dirpath, _, files in os.walk(folder):
        for fn in sorted(files):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, folder)
            res["files"] += 1
            if pats and _hit(rel, pats):
                res["hits"].append((rel, "file or folder name"))
            low = fn.lower()
            try:
                if low.endswith(TEXT_EXT):
                    if os.path.getsize(p) > max_text_mb * 2**20:
                        res["unchecked"].append(rel)
                        continue
                    if pats and _hit(open(p, encoding="utf-8", errors="replace").read(), pats):
                        res["hits"].append((rel, "text content"))
                    res["checked"] += 1
                elif low.endswith(HEADER_EXT):
                    head = open(p, "rb").read(65536).split(b"ElementDataFile")[0].decode("latin-1")
                    if pats and _hit(head, pats):
                        res["hits"].append((rel, "image header"))
                    res["checked"] += 1
                else:
                    try:
                        ds = pydicom.dcmread(p, stop_before_pixels=True)
                    except Exception:
                        res["unchecked"].append(rel)
                        continue
                    res["checked"] += 1
                    for el in ds.iterall():
                        if el.VR in TEXT_VRS or el.tag.is_private:
                            v = el.value
                            if isinstance(v, bytes):
                                v = v.decode("latin-1", "replace")
                            if v not in (None, "") and pats and _hit(v, pats):
                                res["hits"].append((rel, f"DICOM {el.keyword or el.tag}"))
                                break
            except OSError:
                res["unchecked"].append(rel)
    return res


def summary(res, limit=15):
    lines = [f"{res['files']} files, {res['checked']} checked, {len(res['unchecked'])} not checkable, "
             f"{len(res['hits'])} with original identifiers"]
    lines += [f"  ! {rel}: {where}" for rel, where in res["hits"][:limit]]
    if len(res["hits"]) > limit:
        lines.append(f"  ... and {len(res['hits']) - limit} more")
    if res.get("note"):
        lines.append(res["note"])
    return "\n".join(lines)
