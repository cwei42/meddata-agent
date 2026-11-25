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
import shutil
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

    def folder_of(sid):
        """Top folder of a series inside the case (e.g. the plan / fraction folder); names masked by redact."""
        p = files[sid][0].replace("\\", "/")
        return redact(p.split("/")[0]) if "/" in p else "."

    inst_by_path = {i["path"]: i for i in res["instances"]}
    uid_to_id = {inst_by_path[p[0]]["series_uid"]: sid for sid, p in files.items()
                 if p[0] in inst_by_path and inst_by_path[p[0]].get("series_uid")}
    sop_to_id = {i["sop_uid"]: sid for sid, ps in files.items() for p in ps
                 for i in [inst_by_path.get(p)] if i and i.get("sop_uid")}
    refs_cache = {}

    def rt_refs(sid):
        """What an RT object points to: RTSTRUCT -> image series; RTPLAN -> structure set (+ its image);
        RTDOSE -> plan and structure set. Series ids, 'missing' if not in this folder."""
        if sid not in refs_cache:
            out = []
            try:
                ds = pydicom.dcmread(os.path.join(root, files[sid][0]), stop_before_pixels=True)
                for fr in ds.get("ReferencedFrameOfReferenceSequence", []):
                    for st in fr.get("RTReferencedStudySequence", []):
                        for se in st.get("RTReferencedSeriesSequence", []):
                            out.append(uid_to_id.get(str(se.SeriesInstanceUID), "missing"))
                for kw, label, mod in (("ReferencedRTPlanSequence", "plan", "RTPLAN"),
                                       ("ReferencedStructureSetSequence", "structures", "RTSTRUCT")):
                    for it in ds.get(kw, []):
                        rid = sop_to_id.get(str(it.get("ReferencedSOPInstanceUID", "")))
                        if rid:
                            out.append(f"{label} {rid}")
                        else:
                            # the UID points nowhere (some anonymizers break references): same folder instead
                            same = [x["id"] for x in rows if x["modality"] == mod and x["id"] != sid
                                    and folder_of(x["id"]) == folder_of(sid) and folder_of(sid) != "."]
                            rid = same[0] if len(same) == 1 else None
                            out.append(f"{label} {rid} (by folder - UID reference broken)" if rid
                                       else f"{label} not in this data")
                        if label == "plan" and rid:                        # dose -> plan -> structure set
                            out += [x for x in rt_refs(rid) if x.startswith("structures")]
                if ds.get("Modality") == "RTDOSE" and not any(x.startswith("structures") for x in out):
                    same = [x["id"] for x in rows if x["modality"] == "RTSTRUCT"
                            and folder_of(x["id"]) == folder_of(sid) and folder_of(sid) != "."]
                    if len(same) == 1:
                        out.append(f"structures {same[0]} (by folder)")
            except Exception:
                pass
            refs_cache[sid] = list(dict.fromkeys(out))
        return refs_cache[sid]

    @tool("list_series",
          "List series (one line each: id, modality, category, description, file count, slice thickness, "
          "folder = the top folder it came from, e.g. one plan or fraction; refs = what an RT object points to: "
          "RTSTRUCT -> the image series its contours are drawn on, RTPLAN/RTDOSE -> 'plan <id>' and "
          "'structures <id>'). Series ids are numbered in scan order, NOT by fraction - group series of one "
          "plan/fraction by folder. Optional filters narrow the list.",
          {"type": "object", "properties": {
              "modality": {"type": "string", "description": "DICOM code: CT, MR, PT (=PET), NM, US, CR/DX (X-ray), MG, "
                                                            "XA, RTPLAN, RTSTRUCT, RTDOSE, SR, SEG, OT. Several allowed, "
                                                            "e.g. 'CR/DX'. Common names like PET also work."},
              "category": {"type": "string", "description": "image, localizer, derived_image, dose_report, "
                                                            "secondary_capture, structured_report, rt, other"},
              "text": {"type": "string", "description": "case-insensitive substring of description/protocol"},
              "folder": {"type": "string", "description": "case-insensitive substring of the folder, e.g. Fx12"},
              "patient": {"type": "string", "description": "patient pseudonym, e.g. P003"}}})
    def list_series(modality=None, category=None, text=None, patient=None, folder=None):
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
            fol = folder_of(r["id"])
            if folder and folder.lower() not in fol.lower():
                continue
            prot = r["protocol_name"] if r["protocol_name"] and r["protocol_name"] != r["series_description"] else ""
            refs = rt_refs(r["id"]) if r["modality"] in ("RTSTRUCT", "RTPLAN", "RTDOSE") else []
            out.append(f"{r['id']} | {r['modality']} | {r['category']} | {r['series_description'] or '-'}"
                       f"{' / ' + prot if prot else ''} | n={r['n_files']}"
                       f"{'x' + r['frames'] + 'fr' if r['frames'] else ''}"
                       f"{' | thick=' + r['slice_thickness'] if r['slice_thickness'] else ''}"
                       f" | folder={fol}{' | refs=' + ','.join(refs) if refs else ''}"
                       f"{' | PHI-flag' if r['phi_in_text'] else ''}")
        if not out:
            return "no matching series"
        more = f"\n... {len(out) - 80} more (use filters)" if len(out) > 80 else ""
        counts = {}
        for line in out:
            m = line.split(" | ")[1]
            counts[m] = counts.get(m, 0) + 1
        head = f"{len(out)} series (" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ")"
        fols = {}
        for line in out:
            f = line.split(" | folder=")[1].split(" | ")[0] if " | folder=" in line else "."
            fols[f] = fols.get(f, 0) + 1
        if len(fols) > 1:
            def nat(f):
                m = re.search(r"(?i)fx[_ -]?(\d+)", f)
                return (1, int(m.group(1))) if m else (0, 0)
            fx = [f for f in fols if nat(f)[0]]
            head += (f"\n{len(fols)} folders" + (f" ({len(fx)} with a fraction number Fx.., "
                                                f"{len(fols) - len(fx)} without)" if fx else "") + ": " +
                     ", ".join(f"{f} [{fols[f]}]" for f in sorted(fols, key=lambda f: (nat(f), f))))
        return head + ":\n" + "\n".join(out[:80]) + more

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
             "description": r["series_description"], "protocol": r["protocol_name"], "files": n,
             "folder": folder_of(series_id)}
        if r["modality"] == "RTSTRUCT":
            d["contours_drawn_on"] = rt_refs(series_id) or "not stated in the file"
        elif r["modality"] in ("RTPLAN", "RTDOSE"):
            d["references"] = rt_refs(series_id) or "not stated in the file"
        same = [x["id"] for x in rows if x["id"] != series_id and folder_of(x["id"]) == d["folder"]]
        if same and d["folder"] != ".":
            d["other_series_in_same_folder"] = same[:20]
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
    OVERWRITE = {"type": "boolean", "description": "true = replace an earlier result in that folder (only when the "
                                                   "user asked for a new run; the user approves)"}

    def prepare_out(out, overwrite):
        """'' if the folder can be written. An earlier result is replaced only with overwrite=true
        (inside the case folder only, never the originals)."""
        err = O.check_output(root, out)
        if err.startswith("output folder is not empty") and overwrite:
            case = L.case_dir(root)
            if case and os.path.commonpath([case, out]) == case and os.path.abspath(out) != os.path.abspath(case):
                shutil.rmtree(out)
                return ""
        if err.startswith("output folder is not empty"):
            return err + " (an earlier result) - call again with overwrite=true to replace it"
        return err

    @tool("organize_dicom",
          f"Copy all DICOM files into a clean Patient/Study/Series folder structure at {out_dir}. "
          "Originals are not changed. Call with dry_run=true first to see the plan; "
          "dry_run=false performs the copy (the user will be asked to approve).",
          {"type": "object", "properties": {
              "dry_run": {"type": "boolean", "description": "true = only show the plan (default)"},
              "overwrite": OVERWRITE}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def organize_dicom(dry_run=True, overwrite=False):
        if state["plan"] is None:
            state["plan"] = O.make_plan(root, res, tree, rows, files)
        text = O.describe_plan(state["plan"], out_dir)
        if dry_run is not False:
            err = O.check_output(root, out_dir)
            return text + (f"\n\nWARNING: {err}" if err else "\n\n(dry run - nothing copied)")
        err = prepare_out(out_dir, overwrite)
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
          {"type": "object", "properties": {"dry_run": {"type": "boolean", "description": "true = only show plan"},
                                            "overwrite": OVERWRITE}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def anonymize_dicom(dry_run=True, overwrite=False):
        plan = O.make_plan(root, res, tree, rows, files, include_year=False)
        if dry_run is not False:
            err = O.check_output(root, anon_dir)
            return (O.describe_plan(plan, anon_dir) + "\n\nnames/IDs replaced, dates shifted, UIDs remapped, "
                    "free text masked, private tags removed. Pixel data unchanged."
                    + (f"\nWARNING: {err}" if err else "\n(dry run - nothing written)"))
        err = prepare_out(anon_dir, overwrite)
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
                                      "only for RTSTRUCTs whose own referenced series is missing"},
              "overwrite": OVERWRITE}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def export_mha(dry_run=True, reference_series_id=None, reference_modality="auto", overwrite=False):
        if reference_series_id and reference_series_id not in by_id:
            return f"unknown series id {reference_series_id}"
        mod = (reference_modality or "auto").upper().replace("MRI", "MR")
        if mod not in ("AUTO", "CT", "MR"):
            return "reference_modality must be auto, CT or MR"
        refs = {"*": reference_series_id} if reference_series_id else {}
        err = O.check_output(root, mha_dir) if dry_run is not False else prepare_out(mha_dir, overwrite)
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

    def plan_label(sid):
        """Short name of the plan/fraction a series belongs to: 'Fx12' from '..._Fx12_Adapt', else the folder."""
        if sid not in by_id:
            return ""
        f = folder_of(sid)
        m = re.search(r"(?i)(?<![a-z0-9])(fx|fraktion|fraction)[_ -]?(\d+)", f)
        return f"Fx{m.group(2)}" if m else ("plan " + f if f != "." else "")

    def is_baseline(sid):
        return sid in by_id and not re.search(r"(?i)(?<![a-z0-9])(fx|fraktion|fraction)[_ -]?\d+", folder_of(sid))

    def export():
        try:
            ex = MT.Export(mha_dir, derived=derived_dir)
        except FileNotFoundError:
            return None, "no MHA export yet - call export_mha with dry_run=false first"
        for s in ex.structures:
            s["plan"] = plan_label(s["rtstruct"])
        return ex, None

    def pick_sets(ex, structure_set):
        """Keep only the structures of one plan/fraction: an RTSTRUCT id, a folder part like 'Fx30', or
        'baseline' (the folder without a fraction number). Returns (export, error)."""
        if not structure_set:
            return ex, None
        q = str(structure_set).strip()
        sets = sorted({s["rtstruct"] for s in ex.structures if s["rtstruct"] in by_id})
        if q in sets:
            keep = {q}
        elif q.lower() in ("baseline", "plan", "initial", "original plan", "basisplan", "base"):
            keep = {rt for rt in sets if is_baseline(rt)}
        else:
            pat = re.compile(rf"(?i)(?<![a-z0-9]){re.escape(q)}(?![0-9])")
            keep = {rt for rt in sets if pat.search(folder_of(rt)) or pat.search(plan_label(rt))}
        if not keep:
            return None, (f"no structure set matches '{q}'. Structure sets: " +
                          ", ".join(f"{rt} ({plan_label(rt)})" for rt in sets))
        import copy
        ex2 = copy.copy(ex)
        ex2.structures = [s for s in ex.structures if s["rtstruct"] in keep]
        return ex2, None

    def num(s):
        v = [float(x) for x in str(s).split(",") if x.strip()]
        return v[0] if len(v) == 1 else v

    def guarded(fn):
        """Common wrapper: export must exist, structure_set narrows to one plan, errors become text."""
        def run(*a, structure_set=None, **k):
            ex, err = export()
            if err:
                return err
            ex, err = pick_sets(ex, structure_set)
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
    SET = {"type": "string", "description": "one plan/fraction only: e.g. 'Fx30', 'baseline' or an RTSTRUCT id. "
                                            "Without it every structure set (fraction) is included."}
    SETS_NOTE = (" A case can hold one structure set per plan/fraction; each result line starts with "
                 "[RTSTRUCT plan], e.g. [P001-S01-22 Fx30]. Works on the existing MHA export.")

    @tool("structure_stats", "Volume (ml), centroid, extent, number of parts of structures." + SETS_NOTE,
          {"type": "object", "properties": {"select": SEL, "structure_set": SET, "patient": PAT},
           "required": ["select"]})
    @guarded
    def structure_stats(ex, select, patient=None):
        return OPS.stats(ex, select, patient)

    @tool("compare_structures", "Dice, Jaccard, Hausdorff, HD95 and mean surface distance between two structures "
                                "(per structure set when both are in the same set)." + SETS_NOTE,
          {"type": "object", "properties": {"a": SEL, "b": SEL, "structure_set": SET, "patient": PAT},
           "required": ["a", "b"]})
    @guarded
    def compare_structures(ex, a, b, patient=None):
        return OPS.compare(ex, a, b, patient)

    dose_by_folder = {}
    for r in rows:
        if r["modality"] == "RTDOSE":
            dose_by_folder.setdefault(folder_of(r["id"]), r["id"])

    @tool("structure_values", "HU statistics inside a structure, or dose metrics (Dmean, D98/D95/D50/D2, Vx) if "
                              "image_series_id is an RTDOSE. image_series_id='plan_dose' = for each structure the "
                              "RTDOSE of its own plan (usual for dose questions). Default image: the structure's "
                              "reference image. A given RTDOSE is used only with the structure set of its own "
                              "plan." + SETS_NOTE,
          {"type": "object", "properties": {
              "select": SEL, "image_series_id": {"type": "string", "description": "series id, or 'plan_dose'"},
              "v_levels": {"type": "string", "description": "dose levels in Gy for Vx, e.g. '20,30'"},
              "structure_set": SET, "patient": PAT}, "required": ["select"]})
    def structure_values(select, image_series_id=None, v_levels="", structure_set=None, patient=None):
        img = (image_series_id or "").strip()
        note = ""
        if img.lower() in ("plan_dose", "dose", "own_dose", "rtdose"):
            img = lambda s: dose_by_folder.get(folder_of(s["rtstruct"])) if s["rtstruct"] in by_id else None
        elif img in by_id and by_id[img]["modality"] == "RTDOSE" and not structure_set:
            # a dose belongs to one plan: use it with that plan's structure set, not with every fraction
            same = [x["id"] for x in rows if x["modality"] == "RTSTRUCT" and folder_of(x["id"]) == folder_of(img)]
            if same:
                structure_set = same[0]
                note = f"(dose {img} is from {plan_label(img)}: used with its structure set {same[0]})\n"
        v = tuple(float(x) for x in str(v_levels or "").split(",") if x.strip())
        out = guarded(lambda ex: OPS.values(ex, select, img or None, patient, v_levels=v))(
            structure_set=structure_set)
        return note + out

    @tool("merge_structures", f"Combine structures into one new structure (union, intersection or minus), "
                              f"optionally with a margin in mm; saved in {derived_dir}. rtstruct=true also writes "
                              "an RTSTRUCT. Example: all GTVs -> select='GTV*', name='GTV_all'.",
          {"type": "object", "properties": {
              "select": SEL, "name": {"type": "string"},
              "op": {"type": "string", "enum": ["union", "intersection", "minus"]},
              "margin_mm": {"type": "number"}, "rtstruct": {"type": "boolean"}, "structure_set": SET, "patient": PAT},
           "required": ["select", "name"]}, read_only=False)
    @guarded
    def merge_structures(ex, select, name, op="union", margin_mm=0, rtstruct=False, patient=None):
        rt = OPS.rtstruct_writer(root, files, derived_dir) if rtstruct else None
        return OPS.merge(ex, derived_dir, select, name, op or "union", float(margin_mm or 0), patient, rt)

    @tool("expand_structure", "Expand (mm > 0) or shrink (mm < 0) a structure; mm can be 'x,y,z'. "
                              "rtstruct=true also writes an RTSTRUCT.",
          {"type": "object", "properties": {
              "select": SEL, "mm": {"type": "string", "description": "'5', '-3' or '5,5,10'"},
              "name": {"type": "string"}, "rtstruct": {"type": "boolean"}, "structure_set": SET, "patient": PAT},
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
              "name": {"type": "string"}, "structure_set": SET, "patient": PAT}, "required": ["labels"]}, read_only=False)
    @guarded
    def make_labelmap(ex, labels, name="labels", patient=None):
        return OPS.labelmap(ex, derived_dir, str(labels), name or "labels", patient)

    @tool("crop_to_structure", "Crop the reference image and all masks to a structure's box plus margin (mm).",
          {"type": "object", "properties": {"select": SEL, "margin_mm": {"type": "number"}, "structure_set": SET, "patient": PAT},
           "required": ["select"]}, read_only=False)
    @guarded
    def crop_to_structure(ex, select, margin_mm=20, patient=None):
        return OPS.crop(ex, derived_dir, select, float(margin_mm or 20), patient)

    @tool("resample_images", "Resample all images (linear) and masks (nearest) to a new spacing in mm.",
          {"type": "object", "properties": {
              "spacing": {"type": "string", "description": "'1,1,1'"}, "structure_set": SET, "patient": PAT},
           "required": ["spacing"]}, read_only=False)
    @guarded
    def resample_images(ex, spacing, patient=None):
        sp = num(spacing)
        return OPS.resample_all(ex, derived_dir, sp if isinstance(sp, list) else [sp] * 3, patient)

    @tool("export_dvh", "Cumulative DVH (CSV) of structures for an RTDOSE series (used with the structure set "
                        "of the same plan unless structure_set says otherwise).",
          {"type": "object", "properties": {"select": SEL, "dose_series_id": {"type": "string"},
                                            "structure_set": SET, "patient": PAT},
           "required": ["select", "dose_series_id"]}, read_only=False)
    def export_dvh(select, dose_series_id, structure_set=None, patient=None):
        if not structure_set and dose_series_id in by_id:
            same = [x["id"] for x in rows if x["modality"] == "RTSTRUCT"
                    and folder_of(x["id"]) == folder_of(dose_series_id)]
            structure_set = same[0] if same else None
        return guarded(lambda ex: OPS.write_dvh(ex, derived_dir, select, dose_series_id, patient))(
            structure_set=structure_set)

    n_st = sum(len(p["studies"]) for p in tree)
    return Toolset(name="dicom", tools=[list_series, get_series_details, view_thumbnail, list_structures, organize_dicom,
                                        anonymize_dicom, export_mha, structure_stats, compare_structures,
                                        structure_values, merge_structures, expand_structure, make_labelmap,
                                        crop_to_structure, resample_images, export_dvh],
                   redact=redact,
                   describe=f"{len(tree)} patients, {n_st} studies, {len(rows)} series in {root}")
