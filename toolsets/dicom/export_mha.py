"""Export DICOM image series and RTSTRUCTs to MHA using plastimatch.

    OUT/P001/S01_<desc>/
        002_CT_Thorax.mha            image volume (geometry only in header)
        002_CT_Thorax.json           safe technical parameters (no dates, no UIDs, no names)
        005_RTSTRUCT_Plan/
            GTV.mha, PTV.mha, ...    one binary mask per structure, on the reference image grid
            structures.json          structure list + which reference was used
    OUT/export_report.txt            exported / skipped (with reason) / needs review

Reference image for RTSTRUCT (in this order):
  1. user-given: a series id from the scan (e.g. P001-S01-02), a DICOM folder, or an image file (.mha/.nii/.nrrd)
  2. automatic: the series referenced inside the RTSTRUCT (RTReferencedSeriesSequence)
  3. automatic: an image series of the same patient with the same FrameOfReferenceUID (CT preferred)
"""
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile

import pydicom

from . import organize as O
from . import scan as S
from . import structnames as N

PLM_HOME = os.path.expanduser("~/opt/plastimatch")
ITK_LIB = os.path.expanduser("~/opt/itk/lib")
PLASTIMATCH = shutil.which("plastimatch") or os.path.join(PLM_HOME, "bin", "plastimatch")

EXPORT_MODALITIES = {"CT", "MR", "PT", "NM", "CR", "DX", "MG", "RTDOSE"}
SKIP_RISKY = {"US", "OT", "XC", "ES", "SM", "DOC", "SC"}
SAFE_JSON_TAGS = ["Modality", "BodyPartExamined", "ImageType", "SliceThickness", "SpacingBetweenSlices",
                  "MagneticFieldStrength", "ScanningSequence", "SequenceVariant", "SequenceName",
                  "RepetitionTime", "EchoTime", "InversionTime", "FlipAngle", "DiffusionBValue",
                  "ContrastBolusAgent", "KVP", "ConvolutionKernel", "Manufacturer", "ManufacturerModelName",
                  "PatientPosition", "RescaleSlope", "RescaleIntercept", "DoseUnits", "DoseType"]
ALLOWED_MHA_KEYS = {"ObjectType", "NDims", "BinaryData", "BinaryDataByteOrderMSB", "CompressedData",
                    "CompressedDataSize", "TransformMatrix", "Offset", "CenterOfRotation", "AnatomicalOrientation",
                    "ElementSpacing", "DimSize", "ElementNumberOfChannels", "ElementType", "ElementDataFile",
                    "ElementSize", "ElementMin", "ElementMax", "HeaderSize", "Comment", "Modality"}


def available():
    return os.path.exists(PLASTIMATCH)


def _run(args, timeout=1800):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{ITK_LIB}:{PLM_HOME}/lib:" + env.get("LD_LIBRARY_PATH", "")
    env["TMPDIR"] = tempfile.gettempdir()      # inside the encrypted session space (layout.use_session_tmp)
    p = subprocess.run([PLASTIMATCH] + args, capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1][:300] if (p.stderr or p.stdout) else "failed")
    return p.stdout


def _series_dir(root, paths, tmp):
    """Folder of symlinks with exactly one series (plastimatch reads whole folders)."""
    d = tempfile.mkdtemp(dir=tmp)
    for i, p in enumerate(paths):
        os.symlink(os.path.join(root, p), os.path.join(d, f"{i:05d}.dcm"))
    return d


def _clean_header(mha):
    """Rewrite the MHA header keeping only geometry keys (plastimatch/ITK may add metadata)."""
    with open(mha, "rb") as f:
        data = f.read()
    end = data.find(b"ElementDataFile")
    if end < 0:
        raise RuntimeError("unexpected MHA header")
    line_end = data.find(b"\n", end) + 1
    lines = data[:line_end].decode("latin-1").splitlines()
    kept = [l for l in lines if l.split("=")[0].strip() in ALLOWED_MHA_KEYS and not l.startswith("Comment")]
    removed = len(lines) - len(kept)
    if removed:
        with open(mha, "wb") as f:
            f.write(("\n".join(kept) + "\n").encode("latin-1") + data[line_end:])
    return removed


def _safe_json(ds, row, mha_path):
    d = {"id": row["id"], "modality": row["modality"], "category": row["category"],
         "series_number": row["series_number"], "description": row["series_description"],
         "protocol": row["protocol_name"], "n_dicom_files": row["n_files"]}
    for t in SAFE_JSON_TAGS:
        v = S.s(ds, t)
        if v:
            d[t] = v
    try:
        import SimpleITK as sitk
        r = sitk.ImageFileReader()
        r.SetFileName(mha_path)
        r.ReadImageInformation()
        d["size"] = list(r.GetSize())
        d["spacing_mm"] = [round(x, 4) for x in r.GetSpacing()]
    except Exception:
        pass
    return d


def _ref_lookup(rt_path, rt_row, inst_by_series, rows):
    """Find the image series an RTSTRUCT refers to. Returns (series_id, how) or (None, reason)."""
    ds = pydicom.dcmread(rt_path, stop_before_pixels=True)
    ref_uids, for_uids = set(), set()
    for fr in ds.get("ReferencedFrameOfReferenceSequence", []):
        if "FrameOfReferenceUID" in fr:
            for_uids.add(str(fr.FrameOfReferenceUID))
        for st in fr.get("RTReferencedStudySequence", []):
            for se in st.get("RTReferencedSeriesSequence", []):
                ref_uids.add(str(se.SeriesInstanceUID))
    if "FrameOfReferenceUID" in ds:
        for_uids.add(str(ds.FrameOfReferenceUID))
    images = [r for r in rows if r["modality"] in ("CT", "MR", "PT") and r["category"] in ("image", "derived_image")]
    for r in images:
        if inst_by_series[r["id"]]["series_uid"] in ref_uids:
            return r["id"], "referenced series (RTReferencedSeriesSequence)"
    same_for = [r for r in images if inst_by_series[r["id"]]["for_uid"] in for_uids
                and r["patient"] == rt_row["patient"]]
    same_for.sort(key=lambda r: (r["modality"] != "CT", -r["n_files"]))
    if same_for:
        return same_for[0]["id"], "same FrameOfReferenceUID"
    return None, "referenced image series not found in folder - give a reference"


def plan_export(rows):
    """Which series will be exported / skipped and why (no plastimatch needed)."""
    todo, skipped = [], []
    for r in rows:
        if r["modality"] == "RTSTRUCT":
            todo.append((r, "rtstruct"))
        elif r["modality"] in SKIP_RISKY or r["category"] in ("secondary_capture", "dose_report",
                                                              "structured_report", "document"):
            skipped.append((r, "screenshot/report/ultrasound - not a volume, may contain burned-in text"))
        elif r["category"] == "localizer":
            skipped.append((r, "localizer"))
        elif r["modality"] not in EXPORT_MODALITIES:
            skipped.append((r, f"modality {r['modality']} not exported"))
        else:
            todo.append((r, "image"))
    return todo, skipped


def describe(rows, out):
    todo, skipped = plan_export(rows)
    n_img = sum(1 for _, k in todo if k == "image")
    n_rt = sum(1 for _, k in todo if k == "rtstruct")
    lines = [f"MHA export plan -> {out}", f"image series to convert: {n_img}", f"RTSTRUCTs to convert: {n_rt}",
             f"skipped: {len(skipped)}"]
    lines += [f"  - {r['id']} {r['modality']} {r['series_description'] or ''}: {why}" for r, why in skipped[:30]]
    if not available():
        lines.append("WARNING: plastimatch not installed yet")
    return "\n".join(lines)


def run(root, out, res, tree, rows, files, references=None, redact=lambda t: t, only=None,
        standard_names=True, mapping_path=None):
    """references: {rtstruct_series_id or '*': series id | DICOM folder | image file}
    standard_names: name mask files by TG-263 (structnames.py; user mapping file wins)"""
    mapping, map_problems = N.load_mapping(mapping_path) if standard_names else ({}, [])
    unmapped_all = []
    if not available():
        raise RuntimeError(f"plastimatch not found ({PLASTIMATCH})")
    err = O.check_output(root, out)
    if err:
        raise ValueError(err)
    references = references or {}
    plan = O.make_plan(root, res, tree, rows, files, include_year=False)
    dirs = plan["series_dirs"]
    inst_by_path = {i["path"]: i for i in res["instances"]}
    inst_by_series = {sid: inst_by_path[ps[0]] for sid, ps in files.items()}
    by_id = {r["id"]: r for r in rows}
    todo, skipped = plan_export(rows)
    if only:
        todo = [(r, k) for r, k in todo if r["id"] in only]
    os.makedirs(out)
    tmp = tempfile.mkdtemp(prefix="mha_")
    done, failed, rt_done, header_fixes = [], [], [], 0
    exported_img = {}            # series id -> mha path (to reuse as RTSTRUCT reference)
    try:
        # ---- images first (RTSTRUCT may use them as reference)
        for r, kind in todo:
            if kind != "image":
                continue
            paths = files[r["id"]]
            zs = [inst_by_path[p]["slice_z"] for p in paths]
            if len(paths) > 1 and None not in zs and len({round(z, 2) for z in zs}) < len(zs):
                skipped.append((r, "several images per slice position (4D / multi-echo / DWI) - not exported"))
                continue
            dst = os.path.join(out, dirs[r["id"]] + ".mha")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                src = _series_dir(root, paths, tmp)
                if r["modality"] == "RTDOSE":
                    _run(["convert", "--input-dose-img", os.path.join(src, os.listdir(src)[0]), "--output-dose-img", dst])
                else:
                    _run(["convert", "--input", src, "--output-img", dst])
                header_fixes += _clean_header(dst)
                ds = pydicom.dcmread(os.path.join(root, paths[len(paths) // 2]), stop_before_pixels=True)
                meta = _safe_json(ds, r, dst)
                with open(dst[:-4] + ".json", "w", encoding="utf-8") as f:
                    f.write(redact(json.dumps(meta, indent=2, ensure_ascii=False)))
                exported_img[r["id"]] = dst
                done.append(f"{r['id']} -> {os.path.relpath(dst, out)}")
            except Exception as e:
                failed.append(f"{r['id']} ({r['modality']}): {e}")
                if os.path.exists(dst):
                    os.remove(dst)
        # ---- RTSTRUCTs
        for r, kind in todo:
            if kind != "rtstruct":
                continue
            rt_file = os.path.join(root, files[r["id"]][0])
            ref = references.get(r["id"]) or references.get("*")
            how = "given by user"
            if not ref:
                ref, how = _ref_lookup(rt_file, r, inst_by_series, rows)
                if not ref:
                    skipped.append((r, how))
                    continue
            # resolve reference to plastimatch arguments
            if ref in by_id:
                ref_desc = f"series {ref}"
                ref_args = ["--referenced-ct", _series_dir(root, files[ref], tmp)]
            elif os.path.isdir(os.path.expanduser(ref)):
                ref_desc = "DICOM folder (user)"
                ref_args = ["--referenced-ct", os.path.expanduser(ref)]
            elif os.path.isfile(os.path.expanduser(ref)):
                ref_desc = f"image file {os.path.basename(ref)} (user)"
                ref_args = ["--fixed", os.path.expanduser(ref)]
            else:
                skipped.append((r, f"reference '{ref}' not found"))
                continue
            dst_dir = os.path.join(out, dirs[r["id"]])
            os.makedirs(dst_dir, exist_ok=True)
            try:
                raw = os.path.join(tmp, "rt_" + r["id"])
                _run(["convert", "--input", rt_file, *ref_args, "--output-prefix", raw,
                      "--prefix-format", "mha", "--output-ss-list", raw + "_list.txt"])
                structs = []
                try:
                    real = {N.key(n): n for n in N.rtstruct_roi_names(rt_file)}
                except Exception:
                    real = {}
                masks = sorted(glob.glob(os.path.join(raw, "*.mha")))
                stems = [os.path.splitext(os.path.basename(f))[0] for f in masks]
                rois = [real.get(N.key(st), st) for st in stems]
                names = N.standardize_all(rois, mapping) if standard_names else                     [{"original": x, "standard": x, "status": "off", "note": ""} for x in rois]
                for i, (f, roi, nm) in enumerate(zip(masks, rois, names), 1):
                    if nm["status"] == "unmapped":
                        unmapped_all.append(nm)
                    safe = O._safe(redact(nm["standard"]), 60) or f"structure_{i:02d}"
                    target = os.path.join(dst_dir, safe + ".mha")
                    k = 2
                    while os.path.exists(target):
                        target = os.path.join(dst_dir, f"{safe}_{k}.mha")
                        k += 1
                    shutil.move(f, target)
                    header_fixes += _clean_header(target)
                    structs.append({"name": redact(roi), "standard": redact(nm["standard"]),
                                    "naming": nm["status"], "note": redact(nm["note"]),
                                    "file": os.path.basename(target)})
                with open(os.path.join(dst_dir, "structures.json"), "w", encoding="utf-8") as fh:
                    json.dump({"rtstruct": r["id"], "reference": ref_desc, "reference_found_by": how,
                               "reference_image_mha": os.path.relpath(exported_img[ref], dst_dir)
                               if ref in exported_img else None,
                               "structures": structs}, fh, indent=2, ensure_ascii=False)
                n_std = sum(x["naming"] in ("standard", "synonym", "mapping") for x in structs)
                rt_done.append(f"{r['id']} -> {len(structs)} masks on {ref_desc} ({how}); "
                               f"TG-263 names: {n_std} sure, "
                               f"{sum(x['naming'] in ('target', 'helper') for x in structs)} to review, "
                               f"{sum(x['naming'] == 'unmapped' for x in structs)} unmapped")
                for x in structs:
                    if x["naming"] not in ("standard", "off"):
                        rt_done.append(f"    {x['name']} -> {x['standard']} [{x['naming']}] {x['note']}".rstrip())
            except Exception as e:
                failed.append(f"{r['id']} (RTSTRUCT): {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if unmapped_all:   # template for the user: fill in 'standard', add to configs/structure_names.csv
        seen, uniq = set(), []
        for x in unmapped_all:
            if N.key(x["original"]) not in seen:
                seen.add(N.key(x["original"]))
                uniq.append({"original": redact(x["original"])})
        N.write_template(uniq, os.path.join(out, "structure_names_unmapped.csv"))

    n_rt = sum(not x.startswith(" ") for x in rt_done)
    from . import layout as L
    report = [f"MHA export {dt.datetime.now():%Y-%m-%d %H:%M} with plastimatch, orin-agent {L.version()}",
              f"source: {root}", "",
              f"IMAGES exported: {len(done)}", *[f"  {x}" for x in done], "",
              f"RTSTRUCT converted: {n_rt}", *[f"  {x}" for x in rt_done], "",
              f"SKIPPED: {len(skipped)}", *[f"  - {r['id']} {r['modality']}: {why}" for r, why in skipped], "",
              f"FAILED: {len(failed)}", *[f"  ! {x}" for x in failed], "",
              f"header keys removed from MHA files: {header_fixes}",
              *([f"structure names: TG-263 (mapping file: {mapping_path or N.DEFAULT_MAPPING})",
                 *[f"  mapping file problem: {x}" for x in map_problems],
                 *(["  unmapped names kept as they are - fill in structure_names_unmapped.csv and add the rows "
                    "to the mapping file"] if unmapped_all else [])] if standard_names else []),
              "MHA headers contain geometry only. Pixel data unchanged: check images for burned-in text "
              "and 3D head volumes for facial features before sharing."]
    with open(os.path.join(out, "export_report.txt"), "w", encoding="utf-8") as f:
        f.write(redact("\n".join(report)) + "\n")
    return {"images": len(done), "rtstructs": n_rt, "skipped": len(skipped), "failed": failed,
            "out": out, "rt_details": rt_done}
