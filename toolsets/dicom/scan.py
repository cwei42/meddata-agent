#!/usr/bin/env python3
"""
scan.py - Understand the structure of a messy DICOM folder.

Walks a folder recursively, finds every DICOM file regardless of file name or
extension, reads only the headers (no pixel data), and groups the files into
Patient -> Study -> Series. Writes a readable HTML report, a CSV (one row per
series) and a JSON file. Optionally asks a local LLM (llama-server, e.g.
Qwen3.5-4B on the Orin) to interpret each series.

Privacy defaults:
  * Patient names, IDs, birth dates and study dates are NOT written to the
    output; patients are shown as P001, P002, ...
  * If a free-text field (series/study description, protocol name) contains a
    part of the patient's name, it is masked as [NAME] and flagged.
  * The input folder is only read, never modified.
  * Use --show-phi only when the output stays on approved storage.

Usage:
  python cli/report.py INPUT_DIR [-o OUTPUT_DIR] [--llm] [--llm-url URL] [--show-phi]
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import warnings
from collections import defaultdict

import pydicom
from pydicom.errors import InvalidDicomError

from . import uids as U

# real-world exports often contain slightly invalid values; read them quietly
warnings.filterwarnings("ignore", module="pydicom")
pydicom.config.settings.reading_validation_mode = pydicom.config.IGNORE

# ----------------------------------------------------------------- constants
DICOMDIR_UID = "1.2.840.10008.1.3.10"

# SOP Class UID -> (short name, category): hand-set ones; every other SOP class is derived in uids.py
SOP_CATEGORIES = U.OVERRIDES

LOCALIZER_RE = re.compile(r"locali[sz]er|scout|survey|topogram|pilot|tri.?pilot|\bloc\b", re.I)
DOSE_RE = re.compile(r"dose|dosis", re.I)
DERIVED_RE = re.compile(r"\bmpr\b|\bmip\b|\bminip\b|\bvr\b|reformat|recon|_mpr|3d.?rek", re.I)

FREE_TEXT_FIELDS = ("SeriesDescription", "StudyDescription", "ProtocolName")


# ----------------------------------------------------------------- helpers
def s(ds, tag, default=""):
    """Safe string value of a DICOM attribute."""
    v = ds.get(tag, default)
    if v is None:
        return default
    if isinstance(v, pydicom.multival.MultiValue):
        return "\\".join(str(x) for x in v)
    return str(v).strip()


# words that appear in placeholder/anonymised "names" and in normal descriptions
GENERIC_NAME_WORDS = {
    "anonymous", "anonymized", "anonymised", "anon", "patient", "test", "testing", "sample", "samples",
    "image", "images", "phantom", "unknown", "none", "demo", "study", "series", "dicom", "mono", "mono2",
    "rgb", "compressed", "uncompressed", "data", "case", "subject", "volunteer", "qa", "doe", "jane", "john",
    "range", "signed", "unsigned", "slope", "document", "report", "structured", "reporting", "other",
}


def name_tokens(patient_name):
    """Parts of a patient name long enough to be meaningful (>= 3 letters, not generic words)."""
    toks = re.split(r"[\^\s,.\-_=]+", str(patient_name or ""))
    return {t.lower() for t in toks
            if len(t) >= 3 and t.isalpha() and t.lower() not in GENERIC_NAME_WORDS}


def mask_name(text, tokens):
    """Replace occurrences of name tokens in text; return (masked, found)."""
    if not tokens or not text:
        return text, False
    # long name parts (>=5 letters) also match inside words ("Pat.MuellerHans");
    # short ones only as whole words, so "pla" does not hit "RTPLAN"
    alts = []
    for t in sorted(tokens, key=len, reverse=True):
        e = re.escape(t)
        alts.append(e if len(t) >= 5 else rf"(?<![A-Za-zÄÖÜäöüß]){e}(?![A-Za-zÄÖÜäöüß])")
    pat = re.compile("|".join(alts), re.I)
    # never touch existing [NAME] markers (text may be masked twice: tool output, then final answer)
    parts = re.split(r"(\[NAME\])", text)
    n = 0
    for i, p in enumerate(parts):
        if p != "[NAME]":
            parts[i], k = pat.subn("[NAME]", p)
            n += k
    return "".join(parts), n > 0


def read_header(path):
    """Return a pydicom Dataset (headers only) or None if not DICOM."""
    try:
        with open(path, "rb") as f:
            pre = f.read(132)
        has_preamble = len(pre) == 132 and pre[128:132] == b"DICM"
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=not has_preamble)
        # with force=True random files "parse"; require some real DICOM content
        if not has_preamble and "SOPClassUID" not in ds and "Modality" not in ds:
            return None
        return ds
    except (InvalidDicomError, OSError, ValueError, AttributeError, TypeError, EOFError, KeyError):
        return None
    except Exception:
        return None


def classify(series):
    """Rule-based category for a series (no LLM)."""
    sop = series["sop_class_uid"]
    name, cat = U.sop_category(sop)
    text = " ".join([series["series_description"], series["protocol_name"]])
    itype = series["image_type"].upper()
    if cat in ("image", "secondary_capture"):
        # description match alone only for small series: "Planning CT", "Pilot-Studie" ... are real volumes
        few = (series.get("n_files") or 0) * max(int(series.get("frames") or 1), 1) <= 5
        if "LOCALIZER" in itype or (LOCALIZER_RE.search(text) and few):
            cat = "localizer"
        elif cat == "secondary_capture" and DOSE_RE.search(text):
            cat = "dose_report"
        elif "DERIVED" in itype or DERIVED_RE.search(text):
            cat = "derived_image"
    return name, cat


# ----------------------------------------------------------------- scanning
def _slice_z(ds):
    """Position along the slice normal (for sorting slices); None if unknown."""
    try:
        ipp = [float(x) for x in ds.ImagePositionPatient]
        iop = [float(x) for x in ds.ImageOrientationPatient]
        n = (iop[1] * iop[5] - iop[2] * iop[4], iop[2] * iop[3] - iop[0] * iop[5], iop[0] * iop[4] - iop[1] * iop[3])
        return sum(a * b for a, b in zip(ipp, n))
    except Exception:
        return None


def scan(root):
    files_seen = 0
    non_dicom = 0
    dicomdirs = []
    unreadable = []
    instances = []

    t0 = time.time()
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            path = os.path.join(dirpath, fn)
            files_seen += 1
            if files_seen % 500 == 0:
                print(f"  ... {files_seen} files checked", file=sys.stderr)
            ds = read_header(path)
            if ds is None:
                non_dicom += 1
                continue
            msop = s(ds.get("file_meta", {}), "MediaStorageSOPClassUID") if hasattr(ds, "file_meta") else ""
            if msop == DICOMDIR_UID or fn.upper() == "DICOMDIR":
                dicomdirs.append(os.path.relpath(path, root))
                continue
            sop = s(ds, "SOPClassUID") or msop
            st_uid = s(ds, "StudyInstanceUID")
            se_uid = s(ds, "SeriesInstanceUID")
            if not st_uid or not se_uid:
                unreadable.append(os.path.relpath(path, root))
                continue
            instances.append({
                "path": os.path.relpath(path, root),
                "folder": os.path.relpath(dirpath, root),
                "patient_id": s(ds, "PatientID"),
                "patient_name": s(ds, "PatientName"),
                "birth_date": s(ds, "PatientBirthDate"),
                "study_uid": st_uid,
                "study_date": s(ds, "StudyDate"),
                "study_description": s(ds, "StudyDescription"),
                "series_uid": se_uid,
                "series_number": s(ds, "SeriesNumber"),
                "series_description": s(ds, "SeriesDescription"),
                "protocol_name": s(ds, "ProtocolName"),
                "modality": s(ds, "Modality"),
                "body_part": s(ds, "BodyPartExamined"),
                "sop_class_uid": sop,
                "image_type": s(ds, "ImageType"),
                "slice_thickness": s(ds, "SliceThickness"),
                "rows": s(ds, "Rows"),
                "cols": s(ds, "Columns"),
                "frames": s(ds, "NumberOfFrames"),
                "manufacturer": s(ds, "Manufacturer"),
                "model": s(ds, "ManufacturerModelName"),
                "burned_in": s(ds, "BurnedInAnnotation"),
                "transfer_syntax": s(ds.file_meta, "TransferSyntaxUID") if hasattr(ds, "file_meta") else "",
                # per-image fields used by the organizer
                "sop_uid": s(ds, "SOPInstanceUID"),
                "for_uid": s(ds, "FrameOfReferenceUID"),
                "instance_number": s(ds, "InstanceNumber"),
                "slice_z": _slice_z(ds),
                "size": os.path.getsize(path),
            })
    return {
        "files_seen": files_seen, "non_dicom": non_dicom, "dicomdirs": dicomdirs,
        "unreadable": unreadable, "instances": instances, "seconds": time.time() - t0,
    }


def build_tree(instances, show_phi):
    """Group instances into patients -> studies -> series."""
    pats = defaultdict(lambda: {"studies": defaultdict(lambda: {"series": defaultdict(list)})})
    for inst in instances:
        pkey = inst["patient_id"] or inst["patient_name"] or "(no id)"
        pats[pkey]["studies"][inst["study_uid"]]["series"][inst["series_uid"]].append(inst)

    tree = []
    series_rows = []
    files_by_id = {}          # series id -> list of relative file paths (kept out of the outputs)
    for pi, (pkey, p) in enumerate(sorted(pats.items()), 1):
        first = next(iter(next(iter(p["studies"].values()))["series"].values()))[0]
        tokens = name_tokens(first["patient_name"])
        plabel = f"P{pi:03d}"
        pnode = {"label": plabel, "studies": []}
        if show_phi:
            pnode.update(patient_id=first["patient_id"], patient_name=first["patient_name"],
                         birth_date=first["birth_date"])
        for si, (st_uid, st) in enumerate(sorted(p["studies"].items(),
                                                 key=lambda kv: next(iter(kv[1]["series"].values()))[0]["study_date"]), 1):
            sfirst = next(iter(st["series"].values()))[0]
            sdesc, sflag = mask_name(sfirst["study_description"], tokens)
            stnode = {"label": f"{plabel}-S{si:02d}", "study_description": sdesc,
                      "study_year": sfirst["study_date"][:4], "series": [], "phi_in_text": sflag}
            if show_phi:
                stnode.update(study_date=sfirst["study_date"], study_uid=st_uid)
            ser_items = sorted(st["series"].items(),
                               key=lambda kv: (int(kv[1][0]["series_number"]) if kv[1][0]["series_number"].lstrip("-").isdigit() else 9999))
            for k, (se_uid, insts) in enumerate(ser_items, 1):
                # the same image copied twice (another name / folder) counts once: duplicates would otherwise
                # look like 4D data (two images per slice) and break exports
                uniq, seen = [], set()
                for i in sorted(insts, key=lambda i: i["path"]):
                    key = i.get("sop_uid") or i["path"]
                    if key not in seen:
                        seen.add(key)
                        uniq.append(i)
                n_dup, insts = len(insts) - len(uniq), uniq
                f0 = insts[0]
                desc, f1 = mask_name(f0["series_description"], tokens)
                prot, f2 = mask_name(f0["protocol_name"], tokens)
                folders = sorted({i["folder"] for i in insts})
                ex_folder, f3 = mask_name(folders[0], tokens)   # export folders are sometimes named after the patient
                ser = {
                    "id": f"{stnode['label']}-{k:02d}",   # unique; series_number may be missing/duplicated
                    "patient": plabel, "study": stnode["label"],
                    "series_number": f0["series_number"], "modality": f0["modality"],
                    "series_description": desc, "protocol_name": prot,
                    "body_part": f0["body_part"], "image_type": f0["image_type"],
                    "sop_class_uid": f0["sop_class_uid"], "n_files": len(insts), "duplicates": n_dup,
                    "frames": f0["frames"], "slice_thickness": f0["slice_thickness"],
                    "matrix": f"{f0['rows']}x{f0['cols']}" if f0["rows"] else "",
                    "manufacturer": f0["manufacturer"], "model": f0["model"],
                    "burned_in_annotation": f0["burned_in"],
                    "n_folders": len(folders), "example_folder": ex_folder if not show_phi else folders[0],
                    "phi_in_text": f1 or f2 or f3,
                }
                ser["sop_name"], ser["category"] = classify(ser)
                if show_phi:
                    ser["series_uid"] = se_uid
                stnode["series"].append(ser)
                series_rows.append(ser)
                files_by_id[ser["id"]] = [i["path"] for i in insts]
            pnode["studies"].append(stnode)
        tree.append(pnode)
    return tree, series_rows, files_by_id


# ----------------------------------------------------------------- LLM
LLM_PROMPT = """You are helping to understand DICOM series exported from a hospital.
For each series line below, return one JSON object with these keys:
  "id": copy the id exactly,
  "sequence_type": short label, e.g. "T1", "T1 post-contrast", "T2", "FLAIR", "DWI", "ADC", "SWI", "perfusion",
                   "MRA", "CT native", "CT contrast", "CT angiography", "localizer", "dose report", "RT plan",
                   "RT structure set", "RT dose", "screenshot", "other",
  "body_region": best guess (e.g. "head", "thorax", "abdomen", "pelvis", "spine", "whole body", "unknown"),
  "is_primary_diagnostic": true if a clinician would normally read this series, false for localizers, reports, derived/helper data,
  "phi_suspected": true if any text looks like it contains a person's name, date of birth or an ID number,
  "note": max 12 words.
Hints:
- "rule=" is a reliable machine classification. If rule is localizer, rt, dose_report, structured_report,
  document, registration or segmentation, keep that meaning (e.g. rule=localizer -> "localizer").
- Use the description and protocol text first (Siemens/GE/Philips abbreviations, German or English);
  "imagetype" and "sop" help when the description is empty. If there is no information, say "other"/"unknown".
- "[NAME]" means a person name was already removed from the text.
Return ONLY a JSON array, no other text.

Series:
"""


def llm_line(ser):
    parts = [f"id={ser['id']}", f"mod={ser['modality']}", f"desc={ser['series_description']!r}"]
    if ser["protocol_name"] and ser["protocol_name"] != ser["series_description"]:
        parts.append(f"protocol={ser['protocol_name']!r}")
    for k, lab in (("body_part", "bodypart"), ("image_type", "imagetype"), ("sop_name", "sop"),
                   ("slice_thickness", "thick")):
        if ser.get(k):
            parts.append(f"{lab}={ser[k]}")
    parts.append(f"n={ser['n_files']}{'x' + ser['frames'] + 'fr' if ser['frames'] else ''}")
    parts.append(f"rule={ser['category']}")
    return " | ".join(parts)


def parse_json_array(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("no JSON array in answer")
    return json.loads(text[start:end + 1])


def llm_analyze(series_rows, url, batch=20):
    import requests
    results = {}
    total = len(series_rows)
    for i in range(0, total, batch):
        chunk = series_rows[i:i + batch]
        prompt = LLM_PROMPT + "\n".join(llm_line(x) for x in chunk)
        print(f"  LLM: series {i + 1}-{i + len(chunk)} of {total} ...", file=sys.stderr)
        for attempt in (1, 2):
            try:
                r = requests.post(url, json={
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1, "max_tokens": 160 * len(chunk) + 200,
                    "chat_template_kwargs": {"enable_thinking": False},
                }, timeout=600)
                r.raise_for_status()
                arr = parse_json_array(r.json()["choices"][0]["message"]["content"])
                for obj in arr:
                    if isinstance(obj, dict) and "id" in obj:
                        results[str(obj["id"])] = obj
                break
            except Exception as e:
                print(f"  LLM batch failed (attempt {attempt}): {e}", file=sys.stderr)
    for ser in series_rows:
        o = results.get(ser["id"], {})
        ser["llm_sequence_type"] = o.get("sequence_type", "")
        ser["llm_body_region"] = o.get("body_region", "")
        ser["llm_primary"] = o.get("is_primary_diagnostic", "")
        ser["llm_phi_suspected"] = o.get("phi_suspected", "")
        ser["llm_note"] = o.get("note", "")
    return len(results)


# ----------------------------------------------------------------- output
CSV_FIELDS = ["id", "patient", "study", "series_number", "modality", "category", "sop_name",
              "series_description", "protocol_name", "body_part", "n_files", "duplicates", "frames",
              "slice_thickness", "matrix", "image_type", "manufacturer", "model",
              "burned_in_annotation", "phi_in_text", "n_folders", "example_folder",
              "llm_sequence_type", "llm_body_region", "llm_primary", "llm_phi_suspected", "llm_note"]

CAT_COLORS = {
    "image": "#2563eb", "localizer": "#9ca3af", "derived_image": "#7c3aed", "dose_report": "#d97706",
    "secondary_capture": "#db2777", "structured_report": "#d97706", "document": "#d97706",
    "rt": "#059669", "registration": "#0891b2", "segmentation": "#0891b2",
    "presentation_state": "#9ca3af", "multiframe_image": "#60a5fa", "other": "#6b7280",
}


def write_html(path, meta, tree, used_llm):
    e = html.escape
    out = []
    out.append(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>DICOM structure report</title>
<style>
:root{{--bg:#fff;--fg:#1f2937;--mut:#6b7280;--card:#f9fafb;--bd:#e5e7eb;--warn:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111827;--fg:#e5e7eb;--mut:#9ca3af;--card:#1f2937;--bd:#374151;--warn:#f87171}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:1200px}}
h1{{font-size:20px;margin:0 0 4px}} .mut{{color:var(--mut)}}
.sum{{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}} .sum div{{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:8px 12px}}
.sum b{{display:block;font-size:18px}}
details{{border:1px solid var(--bd);border-radius:8px;margin:8px 0;background:var(--card)}}
summary{{cursor:pointer;padding:8px 12px;font-weight:600}}
.study{{margin:0 12px 12px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{border-bottom:1px solid var(--bd);padding:4px 6px;text-align:left;vertical-align:top}}
th{{color:var(--mut);font-weight:500}} .tag{{display:inline-block;color:#fff;border-radius:4px;padding:0 6px;font-size:12px}}
.warn{{color:var(--warn);font-weight:600}} .wrap{{overflow-x:auto}}
</style></head><body>
<h1>DICOM structure report</h1>
<div class="mut">Folder: {e(meta['input'])} &middot; generated {e(meta['generated'])} &middot;
{'patient identifiers shown' if meta['show_phi'] else 'patient identifiers hidden (P001 = pseudonym)'}{' &middot; LLM: ' + e(meta.get('llm_model', 'yes')) if used_llm else ''}</div>
<div class="sum">
<div><b>{meta['files_seen']}</b>files checked</div><div><b>{meta['n_instances']}</b>DICOM objects</div>
<div><b>{meta['n_patients']}</b>patients</div><div><b>{meta['n_studies']}</b>studies</div>
<div><b>{meta['n_series']}</b>series</div><div><b>{meta['non_dicom']}</b>non-DICOM files</div>
<div><b>{len(meta['dicomdirs'])}</b>DICOMDIR index</div>
<div><b class="{'warn' if meta['n_phi_flags'] else ''}">{meta['n_phi_flags']}</b>series with possible PHI in text</div>
</div>""")
    for p in tree:
        n_ser = sum(len(st["series"]) for st in p["studies"])
        extra = f" &middot; {e(p.get('patient_name', ''))} ({e(p.get('patient_id', ''))})" if meta["show_phi"] else ""
        out.append(f"<details open><summary>Patient {p['label']}{extra} &mdash; {len(p['studies'])} studies, {n_ser} series</summary>")
        for st in p["studies"]:
            when = st.get("study_date") if meta["show_phi"] else st["study_year"]
            out.append(f"<div class='study'><p><b>Study {st['label']}</b> &middot; {e(st['study_description'] or '(no description)')}"
                       f" <span class='mut'>&middot; {e(when or 'date ?')}</span>"
                       f"{' <span class=warn>name in description!</span>' if st['phi_in_text'] else ''}</p><div class='wrap'><table><tr>"
                       "<th>#</th><th>Mod</th><th>Category</th><th>Description</th><th>Files</th><th>Thick.</th><th>Matrix</th><th>Type</th>"
                       + ("<th>LLM: sequence</th><th>Region</th><th>Primary</th><th>Note</th>" if used_llm else "")
                       + "<th>Folders</th></tr>")
            for se in st["series"]:
                col = CAT_COLORS.get(se["category"], "#6b7280")
                phi = se["phi_in_text"] or se.get("llm_phi_suspected") is True
                row = (f"<tr><td>{e(se['series_number'])}</td><td>{e(se['modality'])}</td>"
                       f"<td><span class='tag' style='background:{col}'>{e(se['category'])}</span><br><span class='mut'>{e(se['sop_name'])}</span></td>"
                       f"<td>{e(se['series_description'] or '(none)')}{' <span class=warn>⚠ PHI?</span>' if phi else ''}"
                       f"{'<br><span class=mut>' + e(se['protocol_name']) + '</span>' if se['protocol_name'] and se['protocol_name'] != se['series_description'] else ''}</td>"
                       f"<td>{se['n_files']}{' ×' + e(se['frames']) + ' fr' if se['frames'] else ''}</td>"
                       f"<td>{e(se['slice_thickness'])}</td><td>{e(se['matrix'])}</td>"
                       f"<td class='mut'>{e(se['image_type'])}</td>")
                if used_llm:
                    row += (f"<td>{e(str(se.get('llm_sequence_type', '')))}</td><td>{e(str(se.get('llm_body_region', '')))}</td>"
                            f"<td>{e(str(se.get('llm_primary', '')))}</td><td class='mut'>{e(str(se.get('llm_note', '')))}</td>")
                row += f"<td class='mut'>{se['n_folders']}: {e(se['example_folder'])}</td></tr>"
                out.append(row)
            out.append("</table></div></div>")
        out.append("</details>")
    if meta["dicomdirs"] or meta["unreadable"]:
        out.append("<details><summary>Other files</summary><div class='study'>")
        if meta["dicomdirs"]:
            out.append("<p><b>DICOMDIR index files</b> (table of contents of a CD/export, not images):<br>"
                       + "<br>".join(e(x) for x in meta["dicomdirs"]) + "</p>")
        if meta["unreadable"]:
            out.append(f"<p><b>DICOM without study/series UID</b> ({len(meta['unreadable'])}):<br>"
                       + "<br>".join(e(x) for x in meta["unreadable"][:50]) + "</p>")
        out.append("</div></details>")
    out.append("<p class='mut'>Categories are rule-based (SOP class, ImageType, description). "
               "LLM columns are suggestions from a local model and must be checked.</p></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description="Scan a DICOM folder and report its structure.")
    ap.add_argument("input", help="folder to scan (read-only)")
    ap.add_argument("-o", "--output", help="output folder (default: <input>_dicom_report next to the input)")
    ap.add_argument("--llm", action="store_true", help="ask local llama-server to interpret each series")
    ap.add_argument("--llm-url", default="http://127.0.0.1:8080/v1/chat/completions")
    ap.add_argument("--show-phi", action="store_true", help="include patient names/IDs/dates in output")
    args = ap.parse_args()

    root = os.path.abspath(args.input)
    if not os.path.isdir(root):
        sys.exit(f"not a folder: {root}")
    outdir = os.path.abspath(args.output or root.rstrip("/\\") + "_dicom_report")
    if os.path.commonpath([outdir, root]) == root:
        sys.exit("output folder must not be inside the input folder")
    os.makedirs(outdir, exist_ok=True)

    print(f"Scanning {root} ...", file=sys.stderr)
    res = scan(root)
    tree, rows, _files = build_tree(res["instances"], args.show_phi)

    used_llm = False
    llm_model = ""
    if args.llm and rows:
        try:
            import requests
            info = requests.get(args.llm_url.replace("/v1/chat/completions", "/v1/models"), timeout=5).json()
            llm_model = os.path.basename(info["data"][0]["id"])
        except Exception:
            sys.exit("LLM server not reachable - start it first (llm-start.sh)")
        got = llm_analyze(rows, args.llm_url)
        used_llm = True
        print(f"  LLM answered for {got}/{len(rows)} series", file=sys.stderr)

    meta = {
        "input": root, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "show_phi": args.show_phi, "files_seen": res["files_seen"], "non_dicom": res["non_dicom"],
        "dicomdirs": res["dicomdirs"], "unreadable": res["unreadable"],
        "n_instances": len(res["instances"]), "n_patients": len(tree),
        "n_studies": sum(len(p["studies"]) for p in tree), "n_series": len(rows),
        "n_phi_flags": sum(1 for r in rows if r["phi_in_text"] or r.get("llm_phi_suspected") is True),
        "scan_seconds": round(res["seconds"], 1), "llm_model": llm_model,
    }
    write_html(os.path.join(outdir, "report.html"), meta, tree, used_llm)
    with open(os.path.join(outdir, "series.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(outdir, "structure.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "patients": tree}, f, indent=2, ensure_ascii=False)

    print(f"\nDone in {res['seconds']:.1f}s scan: {meta['files_seen']} files, {meta['n_instances']} DICOM objects, "
          f"{meta['n_patients']} patients, {meta['n_studies']} studies, {meta['n_series']} series, "
          f"{len(res['dicomdirs'])} DICOMDIR, {res['non_dicom']} non-DICOM, {meta['n_phi_flags']} PHI flags")
    print(f"Report: {outdir}")


if __name__ == "__main__":
    main()
