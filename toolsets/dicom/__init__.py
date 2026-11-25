"""DICOM toolset: read-only questions about one scanned DICOM folder.

params: {"folder": "/path/to/dicom"}
Only masked, non-identifying information is exposed. `redact` masks every
patient-name part found in the folder, in tool output and in final answers.
"""
import base64
import io
import json
import os
import re
import sys

import numpy as np
import pydicom

from harness import ToolResult, Toolset, tool
from . import layout as L
from . import scan as S

SAFE_TAGS = [
    "Modality", "BodyPartExamined", "ImageType", "SliceThickness", "SpacingBetweenSlices", "PixelSpacing",
    "Rows", "Columns", "NumberOfFrames", "Manufacturer", "ManufacturerModelName", "MagneticFieldStrength",
    "ScanningSequence", "SequenceVariant", "SequenceName", "RepetitionTime", "EchoTime", "InversionTime",
    "FlipAngle", "DiffusionBValue", "ContrastBolusAgent", "KVP", "ConvolutionKernel", "ReconstructionDiameter",
    "PatientPosition", "PhotometricInterpretation", "BitsStored", "BurnedInAnnotation",
]
MOD_ALIASES = {"PET": {"PT"}, "MRI": {"MR"}, "XRAY": {"CR", "DX"}, "X-RAY": {"CR", "DX"}, "RÖNTGEN": {"CR", "DX"},
               "ULTRASOUND": {"US"}, "SONO": {"US"}, "MAMMO": {"MG"}, "ANGIO": {"XA"}, "NUCLEAR": {"NM"},
               "SPECT": {"NM"}, "RT": {"RTPLAN", "RTDOSE", "RTSTRUCT", "RTIMAGE", "RTRECORD"}}


LAST_SCAN = {}   # folder -> (rows, files) of the last scan


def setup(params):
    root = L.input_dir(params["folder"])     # case folder or its original/ -> original/
    L.use_session_tmp(root)                  # temp files stay inside the encrypted space
    if not os.path.isdir(root):
        raise SystemExit(f"DICOM folder not found: {root}")
    print(f"Scanning {root} ...", file=sys.stderr)
    res = S.scan(root)
    tree, rows, files = S.build_tree(res["instances"], show_phi=False)
    by_id = {r["id"]: r for r in rows}
    LAST_SCAN[root] = (rows, files)          # lets the python toolset use the same series ids without rescanning

    all_tokens = set()
    for inst in res["instances"]:
        all_tokens |= S.name_tokens(inst["patient_name"])

    def redact(text):
        return S.mask_name(text, all_tokens)[0] if all_tokens else text

    def read_mid(sid, pixels=False):
        paths = files[sid]
        return pydicom.dcmread(os.path.join(root, paths[len(paths) // 2]), stop_before_pixels=not pixels), len(paths)

    @tool("list_series",
          "List series (one line each: id, modality, category, description, file count, slice thickness). "
          "Optional filters narrow the list.",
          {"type": "object", "properties": {
              "modality": {"type": "string", "description": "DICOM code: CT, MR, PT (=PET), NM, US, CR/DX (X-ray), MG, "
                                                            "XA, RTPLAN, RTSTRUCT, RTDOSE, SR, SEG, OT. Several allowed, "
                                                            "e.g. 'CR/DX'. Common names like PET also work."},
              "category": {"type": "string", "description": "image, localizer, derived_image, dose_report, "
                                                            "secondary_capture, structured_report, rt, other"},
              "text": {"type": "string", "description": "case-insensitive substring of description/protocol"},
              "patient": {"type": "string", "description": "patient pseudonym, e.g. P003"}}})
    def list_series(modality=None, category=None, text=None, patient=None):
        mods = None
        if modality:
            mods = set()
            for k in re.split(r"[/,;|\s]+", modality.strip().upper()):
                if k:
                    mods |= MOD_ALIASES.get(k, {k})
        out = []
        for r in rows:
            if mods and r["modality"].upper() not in mods:
                continue
            if category and r["category"] != category:
                continue
            if patient and r["patient"].upper() != patient.upper():
                continue
            if text and text.lower() not in (r["series_description"] + " " + r["protocol_name"]).lower():
                continue
            prot = r["protocol_name"] if r["protocol_name"] and r["protocol_name"] != r["series_description"] else ""
            out.append(f"{r['id']} | {r['modality']} | {r['category']} | {r['series_description'] or '-'}"
                       f"{' / ' + prot if prot else ''} | n={r['n_files']}"
                       f"{'x' + r['frames'] + 'fr' if r['frames'] else ''}"
                       f"{' | thick=' + r['slice_thickness'] if r['slice_thickness'] else ''}"
                       f"{' | PHI-flag' if r['phi_in_text'] else ''}")
        if not out:
            return "no matching series"
        more = f"\n... {len(out) - 80} more (use filters)" if len(out) > 80 else ""
        return f"{len(out)} series:\n" + "\n".join(out[:80]) + more

    @tool("get_series_details",
          "Technical header fields of one series (TR/TE, slice thickness, orientation, kVp, kernel, contrast ...). "
          "No personal data.",
          {"type": "object", "properties": {"series_id": {"type": "string"}}, "required": ["series_id"]})
    def get_series_details(series_id):
        if series_id not in by_id:
            return f"unknown series id {series_id}"
        r = by_id[series_id]
        ds, n = read_mid(series_id)
        d = {"id": series_id, "category": r["category"], "sop_class": r["sop_name"],
             "description": r["series_description"], "protocol": r["protocol_name"], "files": n}
        for t in SAFE_TAGS:
            v = S.s(ds, t)
            if v:
                d[t] = v[:80]
        iop = ds.get("ImageOrientationPatient")
        if iop is not None and len(iop) == 6:
            nv = np.cross(np.array(iop[:3], float), np.array(iop[3:], float))
            d["orientation"] = ["sagittal", "coronal", "axial"][int(np.argmax(np.abs(nv)))]
        return json.dumps(d, ensure_ascii=False)

    @tool("view_thumbnail", "Look at the middle image of a series (returned to you as a picture).",
          {"type": "object", "properties": {"series_id": {"type": "string"}}, "required": ["series_id"]})
    def view_thumbnail(series_id):
        if series_id not in by_id:
            return f"unknown series id {series_id}"
        try:
            ds, _ = read_mid(series_id, pixels=True)
            a = ds.pixel_array
        except Exception as e:
            return f"no viewable image in this series ({type(e).__name__})"
        if a.ndim == 4 or (a.ndim == 3 and a.shape[-1] not in (3, 4)):
            a = a[a.shape[0] // 2]
        a = a.astype(float)
        if a.ndim == 2:
            try:
                from pydicom.pixels import apply_voi_lut
                a = apply_voi_lut(a, ds).astype(float)
            except Exception:
                pass
            lo, hi = np.percentile(a, (1, 99))
            a = np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1) * 255
            if S.s(ds, "PhotometricInterpretation") == "MONOCHROME1":
                a = 255 - a
        from PIL import Image
        img = Image.fromarray(a.astype(np.uint8)[..., :3] if a.ndim == 3 else a.astype(np.uint8))
        img.thumbnail((448, 448))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return ToolResult(f"thumbnail of {series_id} attached ({img.size[0]}x{img.size[1]})",
                          ["data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()])

    from . import structnames as N
    names_map, _ = N.load_mapping(params.get("structure_names"))

    @tool("list_structures",
          "List the structures (ROI names) of an RTSTRUCT series with their standard AAPM TG-263 name and how "
          "sure the mapping is (standard/mapping/synonym = sure, target/helper = review, unmapped = kept as is).",
          {"type": "object", "properties": {"series_id": {"type": "string", "description": "RTSTRUCT series id"}},
           "required": ["series_id"]})
    def list_structures(series_id):
        r = by_id.get(series_id)
        if not r:
            return f"unknown series id {series_id}"
        if r["modality"] != "RTSTRUCT":
            return f"{series_id} is {r['modality']}, not RTSTRUCT"
        lines = []
        for path in files[series_id]:
            try:
                names = N.rtstruct_roi_names(os.path.join(root, path))
            except Exception as e:
                return f"cannot read RTSTRUCT ({type(e).__name__})"
            for x in N.standardize_all(names, names_map):
                lines.append(f"{x['original']} -> {x['standard']} [{x['status']}] {x['note']}".rstrip())
        return f"{len(lines)} structures:\n" + "\n".join(lines)

    # ---- write tool: organize (output path fixed by config, never chosen by the model)
    out_dir = os.path.abspath(os.path.expanduser(params.get("output") or L.out(root, "organized")))
    from . import organize as O
    state = {"plan": None}

    @tool("organize_dicom",
          f"Copy all DICOM files into a clean Patient/Study/Series folder structure at {out_dir}. "
          "Originals are not changed. Call with dry_run=true first to see the plan; "
          "dry_run=false performs the copy (the user will be asked to approve).",
          {"type": "object", "properties": {
              "dry_run": {"type": "boolean", "description": "true = only show the plan (default)"}}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def organize_dicom(dry_run=True):
        if state["plan"] is None:
            state["plan"] = O.make_plan(root, res, tree, rows, files)
        err = O.check_output(root, out_dir)
        text = O.describe_plan(state["plan"], out_dir)
        if dry_run is not False:
            return text + (f"\n\nWARNING: {err}" if err else "\n\n(dry run - nothing copied)")
        if err:
            return f"cannot organize: {err}"
        r = O.execute(state["plan"], out_dir, mask=redact)
        return (f"done: {r['copied']} files copied to {r['out']}, {r['duplicates']} duplicates skipped, "
                f"{len(r['failed'])} failed verification. manifest.csv and summary.txt written.")

    # ---- write tool: anonymized DICOM copy
    anon_dir = os.path.abspath(os.path.expanduser(params.get("anon_output") or L.out(root, "anonymized")))
    from . import anonymize as A

    @tool("anonymize_dicom",
          f"Write an anonymized DICOM copy (Patient/Study/Series folders) to {anon_dir}. Uses dicognito plus "
          "extra rules; every output file is re-checked. Pixel data unchanged. dry_run=true shows the plan; "
          "dry_run=false writes (the user will be asked to approve).",
          {"type": "object", "properties": {"dry_run": {"type": "boolean", "description": "true = only show plan"}}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def anonymize_dicom(dry_run=True):
        err = O.check_output(root, anon_dir)
        plan = O.make_plan(root, res, tree, rows, files, include_year=False)
        if dry_run is not False:
            return (O.describe_plan(plan, anon_dir) + "\n\nnames/IDs replaced, dates shifted, UIDs remapped, "
                    "free text masked, private tags removed. Pixel data unchanged."
                    + (f"\nWARNING: {err}" if err else "\n(dry run - nothing written)"))
        if err:
            return f"cannot anonymize: {err}"
        r = A.run(root, anon_dir, res, tree, rows, files)
        return (f"done: {r['written']} files written to {r['out']}; verification problems: {len(r['problems'])}; "
                f"failed: {len(r['failed'])}; series to review for burned-in text: {len(r['risky_series'])}. "
                "Details in anonymization_report.txt.")

    # ---- write tool: MHA export with plastimatch
    mha_dir = os.path.abspath(os.path.expanduser(params.get("mha_output") or L.out(root, "mha")))
    from . import export_mha as E

    @tool("export_mha",
          f"Convert image series to MHA and RTSTRUCTs to one MHA mask per structure (plastimatch), into {mha_dir}. "
          "Each RTSTRUCT is put on the image series it references (found automatically) - normally leave "
          "reference_series_id out. reference_modality='CT' or 'MR' puts the masks on the CT or MR of the same "
          "plan instead (use it when the user asks for CT or MR). Pass reference_series_id only when the export "
          "says a reference was not found. dry_run=true shows the plan; dry_run=false writes (the user will be "
          "asked to approve).",
          {"type": "object", "properties": {
              "dry_run": {"type": "boolean", "description": "true = only show plan"},
              "reference_modality": {"type": "string", "enum": ["auto", "CT", "MR"],
                                     "description": "auto (default) = the series each RTSTRUCT names; CT or MR = "
                                                    "that modality from the same plan folder"},
              "reference_series_id": {"type": "string", "description": "optional fallback image series id, used "
                                      "only for RTSTRUCTs whose own referenced series is missing"}}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def export_mha(dry_run=True, reference_series_id=None, reference_modality="auto"):
        if reference_series_id and reference_series_id not in by_id:
            return f"unknown series id {reference_series_id}"
        mod = (reference_modality or "auto").upper().replace("MRI", "MR")
        if mod not in ("AUTO", "CT", "MR"):
            return "reference_modality must be auto, CT or MR"
        refs = {"*": reference_series_id} if reference_series_id else {}
        err = O.check_output(root, mha_dir)
        if dry_run is not False:
            return E.describe(rows, mha_dir) + \
                f"\nreference for RTSTRUCT: {'each its own series' if mod == 'AUTO' else mod + ' of the same plan'}" + \
                (f" (fallback {reference_series_id})" if refs else "") + \
                (f"\nWARNING: {err}" if err else "\n(dry run - nothing written)")
        if err:
            return f"cannot export: {err}"
        r = E.run(root, mha_dir, res, tree, rows, files, references=refs, redact=redact,
                  mapping_path=params.get("structure_names"),
                  reference_modality=None if mod == "AUTO" else mod)
        return (f"done: {r['images']} images, {r['rtstructs']} RTSTRUCTs ({'; '.join(r['rt_details']) or '-'}), "
                f"{r['skipped']} skipped, {len(r['failed'])} failed. See export_report.txt in {r['out']}.")

    # ---- structure tools on the MHA export (deterministic: merge, margins, stats, Dice, dose, labels, ...)
    derived_dir = os.path.abspath(os.path.expanduser(params.get("derived_output") or L.out(root, "structures")))
    from . import mha_tools as MT
    from . import structure_ops as OPS

    def export():
        try:
            return MT.Export(mha_dir, derived=derived_dir), None
        except FileNotFoundError:
            return None, "no MHA export yet - call export_mha with dry_run=false first"

    def num(s):
        v = [float(x) for x in str(s).split(",") if x.strip()]
        return v[0] if len(v) == 1 else v

    def guarded(fn):
        """Common wrapper: export must exist, errors become text for the model."""
        def run(*a, **k):
            ex, err = export()
            if err:
                return err
            try:
                out = fn(ex, *a, **k)
            except (ValueError, FileExistsError) as e:
                return f"not done: {e}"
            if isinstance(out, tuple):
                text, written = out
                return text + (f"\n{len(written)} file(s) written in {derived_dir}" if written else "")
            return out
        return run

    SEL = {"type": "string", "description": "structure name (TG-263 or original) or pattern like 'GTV*'; "
                                            "several with ','"}
    PAT = {"type": "string", "description": "optional patient, e.g. P001"}

    @tool("structure_stats", "Volume (ml), centroid, extent, number of parts of structures. Needs export_mha first.",
          {"type": "object", "properties": {"select": SEL, "patient": PAT}, "required": ["select"]})
    @guarded
    def structure_stats(ex, select, patient=None):
        return OPS.stats(ex, select, patient)

    @tool("compare_structures", "Dice, Jaccard, Hausdorff, HD95 and mean surface distance between two structures.",
          {"type": "object", "properties": {"a": SEL, "b": SEL, "patient": PAT}, "required": ["a", "b"]})
    @guarded
    def compare_structures(ex, a, b, patient=None):
        return OPS.compare(ex, a, b, patient)

    @tool("structure_values", "HU statistics inside a structure, or dose metrics (Dmean, D98/D95/D50/D2, Vx) if "
                              "image_series_id is an RTDOSE. Default image: the reference CT.",
          {"type": "object", "properties": {
              "select": SEL, "image_series_id": {"type": "string"},
              "v_levels": {"type": "string", "description": "dose levels in Gy for Vx, e.g. '20,30'"},
              "patient": PAT}, "required": ["select"]})
    @guarded
    def structure_values(ex, select, image_series_id=None, v_levels="", patient=None):
        v = tuple(float(x) for x in str(v_levels or "").split(",") if x.strip())
        return OPS.values(ex, select, image_series_id, patient, v_levels=v)

    @tool("merge_structures", f"Combine structures into one new structure (union, intersection or minus), "
                              f"optionally with a margin in mm; saved in {derived_dir}. rtstruct=true also writes "
                              "an RTSTRUCT. Example: all GTVs -> select='GTV*', name='GTV_all'.",
          {"type": "object", "properties": {
              "select": SEL, "name": {"type": "string"},
              "op": {"type": "string", "enum": ["union", "intersection", "minus"]},
              "margin_mm": {"type": "number"}, "rtstruct": {"type": "boolean"}, "patient": PAT},
           "required": ["select", "name"]}, read_only=False)
    @guarded
    def merge_structures(ex, select, name, op="union", margin_mm=0, rtstruct=False, patient=None):
        rt = OPS.rtstruct_writer(root, files, derived_dir) if rtstruct else None
        return OPS.merge(ex, derived_dir, select, name, op or "union", float(margin_mm or 0), patient, rt)

    @tool("expand_structure", "Expand (mm > 0) or shrink (mm < 0) a structure; mm can be 'x,y,z'. "
                              "rtstruct=true also writes an RTSTRUCT.",
          {"type": "object", "properties": {
              "select": SEL, "mm": {"type": "string", "description": "'5', '-3' or '5,5,10'"},
              "name": {"type": "string"}, "rtstruct": {"type": "boolean"}, "patient": PAT},
           "required": ["select", "mm"]}, read_only=False)
    @guarded
    def expand_structure(ex, select, mm, name=None, rtstruct=False, patient=None):
        m = num(mm)
        label = ",".join(f"{x:g}" for x in m) if isinstance(m, list) else f"{m:+g}"
        rt = OPS.rtstruct_writer(root, files, derived_dir) if rtstruct else None
        return OPS.merge(ex, derived_dir, select, name or f"{select.strip('*?')}{label}mm", "union", m, patient, rt)

    @tool("make_labelmap", "One multi-label file from several structures (e.g. for deep learning) + label table. "
                           "Later labels win where structures overlap.",
          {"type": "object", "properties": {
              "labels": {"type": "string", "description": "structures=value, e.g. 'PTV=1; GTV*=2' "
                                                          "(';' between labels)"},
              "name": {"type": "string"}, "patient": PAT}, "required": ["labels"]}, read_only=False)
    @guarded
    def make_labelmap(ex, labels, name="labels", patient=None):
        return OPS.labelmap(ex, derived_dir, str(labels), name or "labels", patient)

    @tool("crop_to_structure", "Crop the reference image and all masks to a structure's box plus margin (mm).",
          {"type": "object", "properties": {"select": SEL, "margin_mm": {"type": "number"}, "patient": PAT},
           "required": ["select"]}, read_only=False)
    @guarded
    def crop_to_structure(ex, select, margin_mm=20, patient=None):
        return OPS.crop(ex, derived_dir, select, float(margin_mm or 20), patient)

    @tool("resample_images", "Resample all images (linear) and masks (nearest) to a new spacing in mm.",
          {"type": "object", "properties": {
              "spacing": {"type": "string", "description": "'1,1,1'"}, "patient": PAT},
           "required": ["spacing"]}, read_only=False)
    @guarded
    def resample_images(ex, spacing, patient=None):
        sp = num(spacing)
        return OPS.resample_all(ex, derived_dir, sp if isinstance(sp, list) else [sp] * 3, patient)

    @tool("export_dvh", "Cumulative DVH (CSV) of structures for an RTDOSE series.",
          {"type": "object", "properties": {"select": SEL, "dose_series_id": {"type": "string"}, "patient": PAT},
           "required": ["select", "dose_series_id"]}, read_only=False)
    @guarded
    def export_dvh(ex, select, dose_series_id, patient=None):
        return OPS.write_dvh(ex, derived_dir, select, dose_series_id, patient)

    n_st = sum(len(p["studies"]) for p in tree)
    return Toolset(name="dicom", tools=[list_series, get_series_details, view_thumbnail, list_structures, organize_dicom,
                                        anonymize_dicom, export_mha, structure_stats, compare_structures,
                                        structure_values, merge_structures, expand_structure, make_labelmap,
                                        crop_to_structure, resample_images, export_dvh],
                   redact=redact,
                   describe=f"{len(tree)} patients, {n_st} studies, {len(rows)} series in {root}")
