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
from . import uids as U
from . import journal as J

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
    # Test and phantom data often carry anatomy as "patient name" (LUNG^TG244, boost^breast). Masking those words
    # would destroy every structure name ("Lung_L" -> "[NAME]_L"), so words of the anatomy / RT vocabulary are kept.
    from . import structnames as _N
    vocab = {w for syn in _N.SYNONYMS for w in re.findall(r"[a-z]+", syn)}
    vocab |= {w.lower() for std in _N.STANDARD_NAMES for w in re.findall(r"[A-Za-z][a-z]+|[A-Z]+", std)}
    vocab |= {"ptv", "ctv", "gtv", "itv", "body", "boost", "plan", "test", "phantom", "case", "anon", "thorax",
              "abdomen", "pelvis", "head", "neck", "breast", "chest", "brain", "lung", "lungs", "prostate", "rectum",
              "anal", "bladder", "spine", "tumor", "bed", "nodes", "imrt", "vmat", "sbrt", "and"}
    all_tokens = {t for t in all_tokens if t.lower() not in vocab}

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
    # ---- plans from the DICOM content (never from folder names, which can be anything) ----------------------
    # An RTSTRUCT names its image series; the images, derived images and RTDOSE of one plan share that image's
    # origin (ImagePositionPatient); an RTPLAN carries the same label as its structure set. Each plan is named
    # after its structure set label ("AKZ01_FX26_ADAPT" -> Fx26). Ambiguous matches are left unassigned.
    def head(sid):
        return pydicom.dcmread(os.path.join(root, files[sid][0]), stop_before_pixels=True)

    def own_refs(sid):
        """Image series an RTSTRUCT references by UID."""
        try:
            ds = head(sid)
        except Exception:
            return []
        return [uid_to_id.get(str(se.SeriesInstanceUID), "missing")
                for fr in ds.get("ReferencedFrameOfReferenceSequence", [])
                for st in fr.get("RTReferencedStudySequence", [])
                for se in st.get("RTReferencedSeriesSequence", [])]

    def origin(sid):
        """(x, y, lowest z) of a volume, rounded to 0.1 mm; None for non-volume objects."""
        try:
            ds = head(sid)
            ipp = ds.get("ImagePositionPatient")
            if ipp is None or len(ipp) != 3:
                return None
            zs = [inst_by_path[p]["slice_z"] for p in files[sid] if inst_by_path.get(p, {}).get("slice_z") is not None]
            z = min(zs) if len(zs) > 1 else float(ipp[2])
            return (round(float(ipp[0]), 1), round(float(ipp[1]), 1), round(z, 1))
        except Exception:
            return None

    PLAN = {}               # series id -> RTSTRUCT id of its plan
    PLAN_LABEL = {}         # RTSTRUCT id -> structure set label
    PLAN_HOW = {}           # series id -> how it was matched
    rts = [r["id"] for r in rows if r["modality"] == "RTSTRUCT"]
    by_origin = {}
    for rt in rts:
        try:
            ds = head(rt)
            PLAN_LABEL[rt] = str(ds.get("StructureSetLabel") or ds.get("StructureSetName") or "").strip()
        except Exception:
            PLAN_LABEL[rt] = ""
        PLAN[rt], PLAN_HOW[rt] = rt, "itself"
        refs = [x for x in own_refs(rt) if x in by_id]
        if refs and origin(refs[0]):
            by_origin.setdefault(origin(refs[0]), []).append(rt)
    for r in rows:
        sid = r["id"]
        if r["modality"] in ("RTSTRUCT", "RTPLAN"):
            continue
        o = origin(sid)
        if o and len(by_origin.get(o, [])) == 1:
            PLAN[sid], PLAN_HOW[sid] = by_origin[o][0], "same image origin"
    def uid_ref(sid, kw):
        """Series id of the object a UID reference points to (None if the UID is not in this data)."""
        try:
            for it in head(sid).get(kw, []):
                rid = sop_to_id.get(str(it.get("ReferencedSOPInstanceUID", "")))
                if rid:
                    return rid
        except Exception:
            pass
        return None

    # RTPLAN: its own structure set reference first; the same label only as fallback (broken UIDs)
    for r in rows:
        if r["modality"] == "RTPLAN":
            rt = uid_ref(r["id"], "ReferencedStructureSetSequence")
            if rt in PLAN_LABEL:
                PLAN[r["id"]], PLAN_HOW[r["id"]] = rt, "UID reference"
                continue
            try:
                lab = str(head(r["id"]).get("RTPlanLabel") or "").strip().upper()
            except Exception:
                lab = ""
            hit = [rt for rt in rts if lab and PLAN_LABEL[rt].upper() == lab and r["patient"] == by_id[rt]["patient"]]
            if len(hit) == 1:
                PLAN[r["id"]], PLAN_HOW[r["id"]] = hit[0], "same label"
    # RTDOSE: dose -> plan -> structure set by UID first (overrides the origin match), or a direct reference
    for r in rows:
        if r["modality"] == "RTDOSE":
            pl = uid_ref(r["id"], "ReferencedRTPlanSequence")
            rt = PLAN.get(pl) if pl and PLAN_HOW.get(pl) == "UID reference" else None
            rt = rt or uid_ref(r["id"], "ReferencedStructureSetSequence")
            if rt in PLAN_LABEL:
                PLAN[r["id"]], PLAN_HOW[r["id"]] = rt, "UID reference"
    # a label shared by several structure sets ("RTstruct", "ANON") does not name a plan
    label_count = {}
    for rt, lab in PLAN_LABEL.items():
        label_count[lab.upper()] = label_count.get(lab.upper(), 0) + 1
    for rt, lab in list(PLAN_LABEL.items()):
        if lab and label_count[lab.upper()] > 1 and not re.search(r"(?i)fx[_ -]?\d+", lab):
            PLAN_LABEL[rt] = ""

    refs_cache = {}

    def rt_refs(sid):
        """What an RT object points to: RTSTRUCT -> image series; RTPLAN -> structure set;
        RTDOSE -> plan and structure set. UID references first; if those are broken, the content match."""
        if sid not in refs_cache:
            out = []
            try:
                ds = head(sid)
                out += own_refs(sid)
                for kw, label, mod in (("ReferencedRTPlanSequence", "plan", "RTPLAN"),
                                       ("ReferencedStructureSetSequence", "structures", "RTSTRUCT")):
                    for it in ds.get(kw, []):
                        rid = sop_to_id.get(str(it.get("ReferencedSOPInstanceUID", "")))
                        if rid:
                            out.append(f"{label} {rid}")
                if ds.get("Modality") in ("RTDOSE", "RTPLAN") and PLAN.get(sid):
                    rt, how = PLAN[sid], PLAN_HOW[sid]
                    if not any(x.startswith("structures") for x in out):
                        out.append(f"structures {rt} (by {how})")
                    if ds.get("Modality") == "RTDOSE" and not any(x.startswith("plan") for x in out):
                        pl = [x for x, p in PLAN.items() if p == rt and by_id[x]["modality"] == "RTPLAN"]
                        if len(pl) == 1:
                            out.append(f"plan {pl[0]} (by {how})")
                if ds.get("Modality") in ("RTDOSE", "RTPLAN") and not any(x.startswith("structures") for x in out):
                    out.append("structures not identifiable")
            except Exception:
                pass
            refs_cache[sid] = list(dict.fromkeys(out))
        return refs_cache[sid]

    @tool("list_series",
          "List series (one line each: id, modality, category, description, file count, slice thickness, "
          "plan = the plan/fraction it belongs to, found from the DICOM content (structure set label, image "
          "origin); refs = what an RT object points to; folder = where the files lie, "
          "which can be arbitrary). Series ids are numbered in scan order, NOT by fraction - group by plan=. "
          "Optional filters narrow the list.",
          {"type": "object", "properties": {
              "modality": {"type": "string", "description": "DICOM code: CT, MR, PT (=PET), NM, US, CR/DX (X-ray), MG, "
                                                            "XA, RTPLAN, RTSTRUCT, RTDOSE, SR, SEG, OT. Several allowed, "
                                                            "e.g. 'CR/DX'. Common names like PET also work."},
              "category": {"type": "string", "description": "image, multiframe_image, localizer, derived_image, "
                                                            "dose_report, secondary_capture, structured_report, "
                                                            "document, presentation_state, rt, registration, "
                                                            "segmentation, other"},
              "text": {"type": "string", "description": "case-insensitive substring of description/protocol"},
              "plan": {"type": "string", "description": "only this plan, a name as shown in plan="},
              "folder": {"type": "string", "description": "case-insensitive substring of the folder"},
              "patient": {"type": "string", "description": "patient pseudonym, e.g. P003"}}})
    def list_series(modality=None, category=None, text=None, patient=None, folder=None, plan=None):
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
            pl = plan_label(r["id"])
            if plan and not re.search(rf"(?i)(?<![a-z0-9]){re.escape(plan.strip())}(?![0-9])", pl):
                continue
            prot = r["protocol_name"] if r["protocol_name"] and r["protocol_name"] != r["series_description"] else ""
            refs = rt_refs(r["id"]) if r["modality"] in ("RTSTRUCT", "RTPLAN", "RTDOSE") else []
            if r["modality"] == "RTDOSE":
                size = f"dose grid {r['frames']} planes" if r["frames"] else "dose grid"
            elif r["modality"] in ("RTSTRUCT", "RTPLAN", "SR", "PR", "KO", "REG"):
                size = f"{r['n_files']} file" + ("s" if r["n_files"] != 1 else "")
            elif r["frames"] and r["n_files"] == 1:
                size = f"1 multi-frame file, {r['frames']} frames"
            else:
                size = f"slices={r['n_files']}"
            out.append(f"{r['id']} | {r['modality']} | {r['category']} | {r['series_description'] or '-'}"
                       f"{' / ' + prot if prot else ''} | {size}"
                       f"{' | thick=' + r['slice_thickness'] if r['slice_thickness'] else ''}"
                       f" | plan={pl or '?'}{' | refs=' + ','.join(refs) if refs else ''} | folder={fol}"
                       f"{' | PHI-flag' if r['phi_in_text'] else ''}")
        if not out:
            return "no matching series"
        more = f"\n... {len(out) - 80} more (use filters)" if len(out) > 80 else ""
        counts = {}
        for line in out:
            m = line.split(" | ")[1]
            counts[m] = counts.get(m, 0) + 1
        head = f"{len(out)} series (" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ")"
        plans = {}
        for line in out:
            p_ = line.split(" | plan=")[1].split(" | ")[0] if " | plan=" in line else "?"
            plans[p_] = plans.get(p_, 0) + 1
        known = [p_ for p_ in plans if p_ != "?"]
        if known:
            def nat(p_):
                m = re.match(r"Fx(\d+)$", p_)
                return (1, int(m.group(1))) if m else ((2, 0) if p_ == "?" else (0, 0))
            fx = [p_ for p_ in known if nat(p_)[0] == 1]
            head += (f"\n{len(known)} plans" + (f" ({len(fx)} fractions Fx.., {len(known) - len(fx)} other)"
                                                if fx else "") + ": " +
                     ", ".join(f"{p_} [{plans[p_]} series]" for p_ in sorted(plans, key=lambda p_: (nat(p_), p_))))
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
        if PLAN.get(series_id):
            d["plan"] = plan_label(series_id)
            d["plan_matched_by"] = PLAN_HOW[series_id]
            d["other_series_of_this_plan"] = [x for x, p in PLAN.items() if p == PLAN[series_id] and x != series_id]
        for t in SAFE_TAGS:
            v = S.s(ds, t)
            if v:
                d[t] = v[:80]
        iop = ds.get("ImageOrientationPatient")
        if iop is not None and len(iop) == 6:
            nv = np.cross(np.array(iop[:3], float), np.array(iop[3:], float))
            d["orientation"] = ["sagittal", "coronal", "axial"][int(np.argmax(np.abs(nv)))]
        return json.dumps(d, ensure_ascii=False)

    @tool("lookup_uid",
          "Look up a UID in the DICOM standard dictionary (offline): what a SOP class UID means (CT image, RT "
          "plan, treatment record ...) and its category here, what a transfer syntax is (compressed, lossy), "
          "well-known frames of reference. Also searches by words, e.g. 'RT treatment record' or 'JPEG 2000'. "
          "Instance UIDs (Study/Series/SOP Instance, Frame of Reference) are not in the dictionary.",
          {"type": "object", "properties": {"query": {"type": "string",
                                                      "description": "a UID, or words of its name"}},
           "required": ["query"]})
    def lookup_uid(query):
        res = U.lookup(query)
        uid = res.get("uid")
        if uid and res.get("type") == "SOP Class":
            used = [r["id"] for r in rows if r.get("sop_class_uid") == uid]
            res["series_in_this_case"] = used[:20] if used else "none"
        return json.dumps(res, ensure_ascii=False)

    @tool("show_journal",
          "The operation log of this case: which steps ran (tool, arguments, approval, time), which files they "
          "created or replaced, and what was undone. Use it for questions like 'what did you do?', 'what has "
          "been changed?'. Undoing is done by the user in the console menu, not by you.",
          {"type": "object", "properties": {
              "last": {"type": "integer", "description": "number of newest entries (default 20)"},
              "writes_only": {"type": "boolean", "description": "only steps that wrote files (default false)"}}})
    def show_journal(last=20, writes_only=False):
        case = L.case_dir(root)
        if not case:
            return "no operation log: this folder is not a case in the session space"
        j = J.Journal(case)
        text = j.summary(int(last or 20), kinds=("write", "cli") if writes_only else None)
        nxt = j.last_undoable()
        return text + ("\n\nnext undo (console menu): " + j.describe(nxt) if nxt else "")

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

    ID_FIELDS = ["PatientName", "PatientID", "OtherPatientIDs", "PatientBirthDate", "PatientSex", "PatientAge",
                 "PatientAddress", "PatientTelephoneNumbers", "AccessionNumber", "InstitutionName",
                 "InstitutionAddress", "ReferringPhysicianName", "PerformingPhysicianName", "OperatorsName",
                 "PhysiciansOfRecord", "ReviewerName", "StationName", "DeviceSerialNumber", "StudyDate",
                 "SeriesDate", "AcquisitionDate", "StudyID"]

    @tool("check_identifiers",
          "Check whether the files contain personal identifiers: patient name, ID, birth date, address, accession, "
          "institution, staff names, device serial, dates, private tags. Reports which fields are filled and in how "
          "many series - never the values. Use it for EVERY question about names, IDs, birth dates or anonymization; "
          "the P001-style ids are only labels of this tool, the files themselves may still contain identifiers.")
    def check_identifiers():
        filled = {f: 0 for f in ID_FIELDS}
        other_pn, private, removed, shapes, n = 0, 0, 0, {}, 0
        for sid in files:
            try:
                ds = head(sid)
            except Exception:
                continue
            n += 1
            for f in ID_FIELDS:
                if str(ds.get(f, "") or "").strip():
                    filled[f] += 1
            if str(ds.get("PatientIdentityRemoved", "")).upper() == "YES":
                removed += 1
            pn = str(ds.get("PatientName", "") or "").strip()
            if pn:   # describe the form of the name, never the name itself
                shape = ("code-like (digits / hex)" if re.search(r"\d", pn) or re.fullmatch(r"[0-9a-f-]{16,}", pn)
                         else "starts with ANON" if pn.upper().startswith("ANON")
                         else "looks like a person's name (letters, family^given)")
                shapes[shape] = shapes.get(shape, 0) + 1
            for el in ds.iterall():
                if el.tag.is_private:
                    private += 1
                    break
                if el.VR == "PN" and el.keyword not in ID_FIELDS and str(el.value or "").strip():
                    other_pn += 1
                    break
        lines = [f"{n} series checked (one file each); values are never shown"]
        hit = [f"{f}: filled in {c} of {n} series" for f, c in filled.items() if c]
        lines += hit or ["no identifying field is filled"]
        if shapes:
            lines.append("PatientName form: " + "; ".join(f"{k} in {v} series" for k, v in shapes.items()))
        if other_pn:
            lines.append(f"other person-name fields filled in {other_pn} series")
        lines.append(f"private tags in {private} series; PatientIdentityRemoved=YES in {removed} series")
        strong = [f for f in ("PatientBirthDate", "PatientAddress", "PatientTelephoneNumbers", "OtherPatientIDs")
                  if filled[f]]
        if any(k.startswith("looks like a person") for k in shapes):
            strong.insert(0, "PatientName (name-like)")
        weak = [f for f in ("PatientName", "PatientID", "AccessionNumber") if filled[f] and not
                (f == "PatientName" and strong and strong[0].startswith("PatientName"))]
        if strong:
            verdict = ("contains direct patient identifiers (" + ", ".join(strong) + ") - treat the data as "
                       "identifiable")
        elif weak:
            verdict = ("contains " + ", ".join(weak) + " - these may be pseudonyms or real identifiers; "
                       "the files are not proven anonymous")
        else:
            verdict = "no patient identifiers found in the checked headers"
        lines.append("verdict: " + verdict)
        return "\n".join(lines)

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
                                                   "user asked for a new run; the earlier result goes to the recycle "
                                                   "bin, the user must approve)"}

    def prepare_out(out, overwrite):
        """'' if the folder can be written. An earlier result is replaced only with overwrite=true
        (inside the case folder only, never the originals); it is moved to the recycle bin, not deleted."""
        err = O.check_output(root, out)
        if err.startswith("output folder is not empty") and overwrite:
            case = L.case_dir(root)
            if case and os.path.commonpath([case, out]) == case and os.path.abspath(out) != os.path.abspath(case):
                J.move_to_trash(case, out, os.path.basename(out))
                return ""
        if err.startswith("output folder is not empty"):
            return err + " (an earlier result) - call again with overwrite=true to replace it"
        return err

    @tool("organize_dicom",
          f"Copy all DICOM files into a clean Patient/Study/Series folder structure at {out_dir}. "
          "Originals are not changed. Call with dry_run=true first to see the plan; "
          "dry_run=false performs the copy.",
          {"type": "object", "properties": {
              "dry_run": {"type": "boolean", "description": "true = only show the plan (default)"},
              "overwrite": OVERWRITE}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False,
          deletes=lambda a: a.get("dry_run", True) is False and bool(a.get("overwrite")))
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
          "dry_run=false writes.",
          {"type": "object", "properties": {"dry_run": {"type": "boolean", "description": "true = only show plan"},
                                            "overwrite": OVERWRITE}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False,
          deletes=lambda a: a.get("dry_run", True) is False and bool(a.get("overwrite")))
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
          "says a reference was not found. dry_run=true shows the plan; dry_run=false writes.",
          {"type": "object", "properties": {
              "dry_run": {"type": "boolean", "description": "true = only show plan"},
              "reference_modality": {"type": "string", "enum": ["auto", "CT", "MR"],
                                     "description": "auto (default) = the series each RTSTRUCT names; CT or MR = "
                                                    "that modality from the same plan folder"},
              "reference_series_id": {"type": "string", "description": "optional fallback image series id, used "
                                      "only for RTSTRUCTs whose own referenced series is missing"},
              "overwrite": OVERWRITE}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False,
          deletes=lambda a: a.get("dry_run", True) is False and bool(a.get("overwrite")))
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

    FX = re.compile(r"(?i)(?<![a-z0-9])(fx|fraktion|fraction)[_ -]?(\d+)")

    def plan_label(sid):
        """Name of the plan a series belongs to, from its structure set label: 'Fx12' from 'AKZ01_FX12_ADAPT',
        'baseline <label>' for a plan without fraction number; '' if the series belongs to no known plan."""
        rt = PLAN.get(sid)
        if not rt:
            return ""
        lab = PLAN_LABEL.get(rt, "")
        pat = f"{by_id[rt]['patient']} " if len({r['patient'] for r in rows}) > 1 else ""   # several patients
        m = FX.search(lab)
        if m:
            return f"{pat}Fx{m.group(2)}"
        # 'baseline' only makes sense next to adaptive fractions; otherwise just the plan's label
        word = "baseline" if any(FX.search(x) for x in PLAN_LABEL.values()) else "plan"
        return f"{pat}{word} {lab}".strip() if lab else f"plan of {rt}"

    def is_baseline(sid):
        return plan_label(sid).startswith("baseline")

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
            keep = {rt for rt in sets if pat.search(plan_label(rt)) or pat.search(PLAN_LABEL.get(rt, ""))}
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
                                            "several with ','. 'OARS' = all organs at risk, 'TARGETS' = all "
                                            "GTV/CTV/ITV/PTV"}
    PAT = {"type": "string", "description": "optional patient, e.g. P001"}
    SET = {"type": "string", "description": "one plan only: a plan name as list_series shows it, or an RTSTRUCT id. "
                                            "Without it every structure set (fraction) is included."}
    SETS_NOTE = (" A case can hold one structure set per plan/fraction; each result line starts with "
                 "[RTSTRUCT plan]. Works on the existing MHA export.")

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

    dose_by_plan = {}                          # RTSTRUCT id -> the RTDOSE of the same plan (content match)
    for r in rows:
        if r["modality"] == "RTDOSE" and PLAN.get(r["id"]):
            dose_by_plan.setdefault(PLAN[r["id"]], []).append(r["id"])
    dose_by_plan = {k: v[0] for k, v in dose_by_plan.items() if len(v) == 1}

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
            img = lambda s: dose_by_plan.get(s["rtstruct"])
        elif img in by_id and by_id[img]["modality"] == "RTDOSE" and not structure_set:
            # a dose belongs to one plan: use it with that plan's structure set, not with every fraction
            same = [PLAN[img]] if PLAN.get(img) else []
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
            same = [PLAN[dose_series_id]] if PLAN.get(dose_series_id) else []
            structure_set = same[0] if same else None
        return guarded(lambda ex: OPS.write_dvh(ex, derived_dir, select, dose_series_id, patient))(
            structure_set=structure_set)

    n_st = sum(len(p["studies"]) for p in tree)
    return Toolset(name="dicom", tools=[list_series, get_series_details, lookup_uid, show_journal, view_thumbnail, list_structures,
                                        check_identifiers, organize_dicom,
                                        anonymize_dicom, export_mha, structure_stats, compare_structures,
                                        structure_values, merge_structures, expand_structure, make_labelmap,
                                        crop_to_structure, resample_images, export_dvh],
                   redact=redact,
                   describe=f"{len(tree)} patients, {n_st} studies, {len(rows)} series in {root}")
