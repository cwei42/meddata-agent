"""High-level structure operations on an MHA export (shared by cli/structures.py and the agent tools).

Every function works per patient (all patients, or one with patient=...), writes only into out_dir
(default <dataset>_derived), never overwrites, and returns a text report. Results that are files get a JSON
sidecar saying exactly what was done (inputs by name, operation, margin, volumes).
"""
import csv
import datetime as dt
import json
import os

from . import mha_tools as T


def rtstruct_writer(root, files, out_dir):
    """f(patient, masks{name: img}, ref_series_id) -> path: new RTSTRUCT on the reference CT (plastimatch).
    root/files: the scanned DICOM dataset (series id -> file list)."""
    import shutil
    import tempfile
    from . import export_mha as E

    def write(patient, masks, ref_id):
        if not ref_id or ref_id not in files:
            raise ValueError("reference CT unknown - RTSTRUCT not written")
        dst = os.path.join(out_dir, patient, "RTSTRUCT_" + "_".join(_safe(n) for n in masks))
        if os.path.exists(dst):
            raise FileExistsError(dst)
        tmp = tempfile.mkdtemp(prefix="ref_")
        try:
            return T.masks_to_rtstruct(masks, E._series_dir(root, files[ref_id], tmp), dst, E._run)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return write


def default_out(export_folder):
    base = os.path.abspath(export_folder).rstrip("/")
    return (base[:-4] if base.endswith("_mha") else base) + "_derived"


def _patients(ex, patient):
    ps = sorted({s["patient"] for s in ex.structures} | {i["patient"] for i in ex.images})
    if patient:
        if patient not in ps:
            raise ValueError(f"patient {patient} not in the export (have: {', '.join(ps) or '-'})")
        return [patient]
    return ps


def _sidecar(path, info):
    from . import layout as L
    info = dict(info, written=f"{dt.datetime.now():%Y-%m-%d %H:%M}", tool=f"orin-agent {L.version()} structure_ops")
    with open(path[:-4] + ".json", "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)


def _names(sel):
    return [s["name"] for s in sel]


def _tag(s):
    """Which structure set (and plan/fraction) a structure comes from - a case can hold one per fraction."""
    if s.get("rtstruct") in (None, "", "derived"):
        return "[derived]" if s.get("rtstruct") == "derived" else ""
    return f"[{s['rtstruct']}{' ' + s['plan'] if s.get('plan') else ''}]"


def _sets_note(sel):
    sets = sorted({s["rtstruct"] for s in sel if s.get("rtstruct") not in (None, "", "derived")})
    return (f"note: combines structures from {len(sets)} structure sets ({', '.join(sets)}), i.e. different "
            f"plans/fractions - pass structure_set to use one") if len(sets) > 1 else ""


def _safe(name):
    import re
    return re.sub(r"[^A-Za-z0-9_+\-^.]+", "_", name).strip("_")[:60] or "structure"


# ------------------------------------------------------------------ read-only
def list_structures(ex, patient=None):
    lines = []
    for p in _patients(ex, patient):
        ss = [s for s in ex.structures if s["patient"] == p]
        ims = [i for i in ex.images if i["patient"] == p]
        lines.append(f"{p}: images " + ", ".join(f"{i['id']} {i['modality']}" for i in ims))
        for s in ss:
            lines.append(f"  {s['rtstruct']}  {s['name']} -> {s['standard']} [{s['naming']}]")
    return "\n".join(lines) or "no structures in the export"


def stats(ex, select, patient=None):
    lines = []
    for p in _patients(ex, patient):
        for s in ex.find(select, patient=p):
            st = T.structure_stats(ex.mask(s))
            lines.append(f"{p} {_tag(s)} {s['name']} ({s['standard']}): " +
                         ", ".join(f"{k}={v}" for k, v in st.items()))
    return "\n".join(lines) or f"no structure matches '{select}' (list_structures shows the names; select='OARS' = all organs, 'TARGETS' = all targets)"


def compare(ex, a, b, patient=None):
    """Per structure set when both are in the same set (one result per fraction); otherwise (e.g. GTV of the
    plan vs GTV of Fx30) the two selections are compared as they are."""
    lines = []
    for p in _patients(ex, patient):
        sa, sb = ex.find(a, patient=p), ex.find(b, patient=p)
        if not sa or not sb:
            continue
        sets = [rt for rt in dict.fromkeys(s["rtstruct"] for s in sa + sb)]
        pairs = [([s for s in sa if s["rtstruct"] == rt], [s for s in sb if s["rtstruct"] == rt]) for rt in sets]
        pairs = [(x, y) for x, y in pairs if x and y] or [(sa, sb)]
        for xa, xb in pairs:
            ma = T.combine([ex.mask(s) for s in xa])[0]
            mb = T.combine([ex.mask(s) for s in xb])[0]
            r = T.compare(ma, mb)
            ta = " ".join(sorted({_tag(s) for s in xa}))
            tb = " ".join(sorted({_tag(s) for s in xb}))
            lines.append(f"{p} {ta} {'+'.join(_names(xa))} vs {tb} {'+'.join(_names(xb))}: " +
                         ", ".join(f"{k}={v}" for k, v in r.items()))
    return "\n".join(lines) or f"'{a}' and '{b}' not both found for any patient"


def values(ex, select, image_id=None, patient=None, v_levels=(), d_levels=(98, 95, 50, 2)):
    """HU statistics (CT/MR/PT) or dose metrics (RTDOSE) inside structures. image_id: series id from the
    export, or a function structure -> series id (e.g. the dose of the structure's own plan);
    default: the structure's reference image."""
    lines = []
    for p in _patients(ex, patient):
        for s in ex.find(select, patient=p):
            iid = image_id(s) if callable(image_id) else image_id
            if callable(image_id) and not iid:
                lines.append(f"{p} {_tag(s)} {s['name']}: no RTDOSE identified for this plan")
                continue
            img = ex.image(iid) if iid else (ex.image(s["reference_id"]) if s["reference_id"] else None)
            if not img and s.get("reference_mha") and not iid:
                img = {"id": "reference", "modality": "", "file": s["reference_mha"]}
            if not img:
                lines.append(f"{p} {_tag(s)} {s['name']}: image {iid or '(reference)'} not in the export")
                continue
            if img.get("patient") and img["patient"] != p:
                lines.append(f"{p} {s['name']}: {img['id']} belongs to another patient - skipped")
                continue
            if img["modality"] == "RTDOSE":
                r = T.dose_stats(img["file"], ex.mask(s), d_levels=d_levels, v_levels=v_levels)
            else:
                r = T.image_stats(img["file"], ex.mask(s))
            lines.append(f"{p} {_tag(s)} {s['name']} in {img['id']} {img['modality']}: " +
                         ", ".join(f"{k}={v}" for k, v in r.items()))
    return "\n".join(lines) or f"no structure matches '{select}' (list_structures shows the names; select='OARS' = all organs, 'TARGETS' = all targets)"


# ------------------------------------------------------------------ writing
def merge(ex, out_dir, select, name, op="union", margin_mm=0, patient=None, rtstruct=None):
    """Combine the selected structures per patient (union/intersection/minus), optionally add a margin, write
    <out>/<patient>/<name>.mha (+ .json). rtstruct: callable(patient, masks{name: img}, ref_id) -> path."""
    lines, written = [], []
    for p in _patients(ex, patient):
        sel = ex.find(select, patient=p)
        if not sel:
            lines.append(f"{p}: nothing matches '{select}' - skipped")
            continue
        m, notes = T.combine([ex.mask(s) for s in sel], op)
        if margin_mm:
            m = T.margin(m, margin_mm)
        path = os.path.join(out_dir, p, _safe(name) + ".mha")
        T.write(m, path)
        st = T.structure_stats(m)
        _sidecar(path, {"patient": p, "name": name, "operation": op, "inputs": _names(sel),
                        "inputs_tg263": [s["standard"] for s in sel], "margin_mm": margin_mm,
                        "reference_series": sel[0]["reference_id"], "notes": notes, "stats": st})
        written.append(path)
        mtxt = (f" margin {','.join(f'{v:g}' for v in margin_mm)} mm (x,y,z)" if isinstance(margin_mm, (list, tuple))
                else f" {'+' if margin_mm > 0 else '-'} {abs(margin_mm):g} mm") if margin_mm else ""
        lines.append(f"{p}: {name} = {op}({', '.join(_names(sel))})" + mtxt +
                     f" -> {st['volume_ml']} ml, {st.get('components', 0)} part(s)" +
                     (f"; {'; '.join(notes)}" if notes else "") + f" -> {os.path.relpath(path, out_dir)}")
        if _sets_note(sel):
            lines.append("    " + _sets_note(sel))
        if rtstruct:
            try:
                rs = rtstruct(p, {name: m}, sel[0]["reference_id"])
                lines.append(f"    RTSTRUCT: {os.path.relpath(rs, out_dir)}")
            except Exception as e:
                lines.append(f"    RTSTRUCT failed: {e}")
    return "\n".join(lines), written


def parse_labels(text):
    """Label specification -> [(label_name, select, value or None)], in order. Accepted forms:
        'GTV=GTV*, PTV=PTV'        name = structures   (values 1, 2 by order)
        'PTV=1, GTV*=2'            structures = value
        '1=PTV; 2=GTV,GTVn1'       value = structures  (';' between labels allows ',' inside a selection)
        'PTV=1,GTV,GTVn1,GTV2_Ln=2'  bare names are joined to the next label
    """
    text = str(text or "").strip()
    parts = [x.strip() for x in (text.split(";") if ";" in text else text.split(",")) if x.strip()]
    out, pending = [], []
    for part in parts:
        sep = "=" if "=" in part else (":" if ":" in part else None)
        if not sep:
            pending.append(part)
            continue
        left, right = (x.strip() for x in part.split(sep, 1))
        if right.isdigit():
            sel, value = ",".join(pending + [left]), int(right)
            lab = sel.split(",")[0].strip("*?_ ") or sel
        elif left.isdigit():
            sel, value = ",".join(pending + [right]), int(left)
            lab = sel.split(",")[0].strip("*?_ ") or sel
        else:
            lab, sel, value = left, ",".join(pending + [right]), None
        pending = []
        out.append((lab, sel, value))
    out += [(p.strip("*?_ ") or p, p, None) for p in pending]     # bare names left over: one label each
    return out


def labelmap(ex, out_dir, labels, name="labels", patient=None):
    """labels: text (see parse_labels) or [(label_name, select[, value])] in priority order (later wins where
    structures overlap). Writes <out>/<patient>/<name>.mha + <name>.json with the label table."""
    if isinstance(labels, str):
        labels = parse_labels(labels)
    if not labels:
        raise ValueError("no labels given, e.g. 'PTV=1, GTV*=2'")
    lines, written = [], []
    for p in _patients(ex, patient):
        items, missing = [], []
        for spec in labels:
            lab, sel = spec[0], spec[1]
            value = spec[2] if len(spec) > 2 else None
            found = ex.find(sel, patient=p)
            if not found:
                missing.append(f"{lab} ({sel})")
                continue
            items.append((lab, T.combine([ex.mask(s) for s in found])[0], _names(found), value))
        if not items:
            lines.append(f"{p}: none of the labels found ({', '.join(missing)}) - skipped")
            continue
        img, table, over = T.labelmap([(lab, m, v) for lab, m, _, v in items])
        path = os.path.join(out_dir, p, _safe(name) + ".mha")
        T.write(img, path)
        _sidecar(path, {"patient": p, "labels": {str(k): v for k, v in table.items()},
                        "sources": {lab: src for lab, _, src, _ in items}, "missing": missing,
                        "overlap_voxels_overwritten": over, "priority": "later labels overwrite earlier ones"})
        written.append(path)
        lines.append(f"{p}: {name} " + ", ".join(f"{k}={v}" for k, v in table.items() if k) +
                     (f"; missing: {', '.join(missing)}" if missing else "") +
                     (f"; overlaps overwritten: {over}" if over else "") + f" -> {os.path.relpath(path, out_dir)}")
    return "\n".join(lines), written


def crop(ex, out_dir, select, margin_mm=20, patient=None, image_id=None, with_masks=True):
    """Crop the reference image (or image_id) and all structure masks of the patient to the bounding box of the
    selected structures + margin (e.g. patches for deep learning)."""
    lines, written = [], []
    for p in _patients(ex, patient):
        sel = ex.find(select, patient=p)
        if not sel:
            lines.append(f"{p}: nothing matches '{select}' - skipped")
            continue
        box = T.combine([ex.mask(s) for s in sel])[0]
        img = ex.image(image_id) if image_id else ex.image(sel[0]["reference_id"] or "")
        targets = ([(img["id"], img["file"], False)] if img else []) + \
            ([(f"mask_{s['standard']}", s["file"], True) for s in ex.structures if s["patient"] == p]
             if with_masks else [])
        n = 0
        for label, f, is_mask in targets:
            src = T.read_mask(f) if is_mask else T.read_image(f)
            out = T.crop(src, box, margin_mm)
            path = os.path.join(out_dir, p, f"crop_{_safe(select)}", _safe(label) + ".mha")
            T.write(out, path)
            written.append(path)
            n += 1
        size = T.crop(T.read_image(targets[0][1]), box, margin_mm).GetSize() if targets else None
        lines.append(f"{p}: {n} files cropped to {select} + {margin_mm} mm, size {size} -> "
                     f"{os.path.join(p, 'crop_' + _safe(select))}")
    return "\n".join(lines), written


def resample_all(ex, out_dir, spacing, patient=None):
    """Resample every image (linear) and mask (nearest) of the patient(s) to a new spacing, e.g. (1, 1, 1)."""
    lines, written = [], []
    for p in _patients(ex, patient):
        n = 0
        for i in [i for i in ex.images if i["patient"] == p]:
            path = os.path.join(out_dir, p, f"resampled_{'x'.join(f'{v:g}' for v in spacing)}", _safe(i["id"]) + ".mha")
            written.append(T.write(T.resample(i["file"], spacing=spacing), path))
            n += 1
        for s in [s for s in ex.structures if s["patient"] == p]:
            path = os.path.join(out_dir, p, f"resampled_{'x'.join(f'{v:g}' for v in spacing)}",
                                "mask_" + _safe(s["standard"]) + ".mha")
            written.append(T.write(T.resample(s["file"], spacing=spacing, mask=True), path))
            n += 1
        lines.append(f"{p}: {n} files resampled to {spacing} mm")
    return "\n".join(lines), written


def write_dvh(ex, out_dir, select, dose_id, patient=None, step=0.1):
    lines, written = [], []
    dose = ex.image(dose_id)
    if not dose or dose["modality"] != "RTDOSE":
        return f"{dose_id} is not an RTDOSE in the export", []
    for p in _patients(ex, patient or dose["patient"]):
        for s in ex.find(select, patient=p):
            rows = T.dvh(dose["file"], ex.mask(s), step)
            tag = "" if s.get("rtstruct") in (None, "", "derived") else _safe(s["rtstruct"]) + "_"
            path = os.path.join(out_dir, p, f"dvh_{_safe(dose_id)}_{tag}{_safe(s['standard'])}.csv")
            if os.path.exists(path):
                raise FileExistsError(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["dose_Gy", "volume_percent"])
                w.writerows(rows)
            written.append(path)
            lines.append(f"{p} {_tag(s)} {s['name']}: DVH {len(rows)} points -> {os.path.relpath(path, out_dir)}")
    return "\n".join(lines) or f"no structure matches '{select}' (list_structures shows the names; select='OARS' = all organs, 'TARGETS' = all targets)", written
