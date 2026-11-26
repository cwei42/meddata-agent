"""Copy a messy DICOM folder into a clean Patient/Study/Series structure.

    OUT/
      P001/
        S01_2024_MR_Kopf/
          001_MR_t1_mprage_sag/IM0001.dcm ...   (sorted by slice position / instance number)
          002_MR_localizer/...
      manifest.csv      source -> destination for every copied file
      summary.txt       what was done

Rules: originals are only read (copy, never move); output must not be inside the
input; free space is checked first; every copy is verified by size; duplicate
SOPInstanceUIDs are copied once. Folder names use pseudonyms (P001) and masked
descriptions. NOTE: this organizes, it does not anonymize - the copied files still
contain all original header data.
"""
import csv
import datetime as dt
import os
import re
import shutil

from . import scan as S


def _safe(text, maxlen=40):
    text = re.sub(r"\[NAME\]", "", text or "")
    text = re.sub(r"[^A-Za-z0-9äöüÄÖÜß.+-]+", "_", text).strip("_.")
    return text[:maxlen].rstrip("_") or ""


def _num(v, default=10**9):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def make_plan(root, res, tree, rows, files_by_id, show_phi=False, include_year=True):
    """Return dict with 'items' [(src_rel, dst_rel, size)], 'duplicates', 'series' summary lines.
    include_year=False for anonymized output (folder names must not carry the real study year)."""
    inst_by_path = {i["path"]: i for i in res["instances"]}
    row_by_id = {r["id"]: r for r in rows}
    items, dups, series_lines = [], [], []
    series_dirs = {}            # series id -> relative folder (used by the MHA export)
    seen_sop = {}
    for p in tree:
        for st in p["studies"]:
            sidx = st["label"].split("-")[-1]                      # S01
            parts = [sidx, (st.get("study_year") or "") if include_year else "", _safe(st["study_description"], 30)]
            st_dir = "_".join(x for x in parts if x)
            used_dirs = set()
            for ser in st["series"]:
                r = row_by_id[ser["id"]]
                desc = _safe(r["series_description"]) or _safe(r["protocol_name"]) or _safe(r["sop_name"]) or r["category"]
                num = r["series_number"] if r["series_number"].lstrip("-").isdigit() else ser["id"].split("-")[-1]
                se_dir = f"{int(num):03d}_{_safe(r['modality'], 10) or 'XX'}_{desc}" if num.lstrip("-").isdigit() \
                    else f"{num}_{r['modality']}_{desc}"
                base, n = se_dir, 2
                while se_dir.lower() in used_dirs:          # two series with same number+description
                    se_dir, n = f"{base}_{n}", n + 1
                used_dirs.add(se_dir.lower())
                series_dirs[ser["id"]] = os.path.join(p["label"], st_dir, se_dir)
                insts =[inst_by_path[f] for f in files_by_id[ser["id"]]]
                insts.sort(key=lambda i: (_num(i["slice_z"]) if i["slice_z"] is not None else 10**9,
                                          _num(i["instance_number"]), i["path"]))
                # if positions are missing, fall back to instance number order
                if all(i["slice_z"] is None for i in insts):
                    insts.sort(key=lambda i: (_num(i["instance_number"]), i["path"]))
                k = 0
                for i in insts:
                    if i["sop_uid"] and i["sop_uid"] in seen_sop:
                        dups.append((i["path"], seen_sop[i["sop_uid"]]))
                        continue
                    k += 1
                    dst = os.path.join(p["label"], st_dir, se_dir, f"IM{k:04d}.dcm")
                    if i["sop_uid"]:
                        seen_sop[i["sop_uid"]] = dst
                    items.append((i["path"], dst, i["size"]))
                series_lines.append(f"{p['label']}/{st_dir}/{se_dir}  ({k} files)")
    # copies of the same image that the scan already folded into one (other name / folder): list them too
    in_series = {f for fs in files_by_id.values() for f in fs}
    for i in res["instances"]:
        if i["path"] not in in_series and i["sop_uid"] in seen_sop:
            dups.append((i["path"], seen_sop[i["sop_uid"]]))
    total = sum(x[2] for x in items)
    return {"root": root, "items": items, "duplicates": dups, "series": series_lines,
            "total_bytes": total, "show_phi": show_phi, "series_dirs": series_dirs}


def describe_plan(plan, out, max_lines=40):
    lines = [f"Plan: copy {len(plan['items'])} files ({plan['total_bytes'] / 2**20:.1f} MB) "
             f"into {len(plan['series'])} series folders under {out}",
             f"duplicates skipped: {len(plan['duplicates'])}", ""]
    lines += plan["series"][:max_lines]
    if len(plan["series"]) > max_lines:
        lines.append(f"... {len(plan['series']) - max_lines} more series")
    return "\n".join(lines)


def check_output(root, out):
    root, out = os.path.abspath(root), os.path.abspath(out)
    if os.path.commonpath([root, out]) == root:
        return "output folder must not be inside the input folder"
    if os.path.commonpath([root, out]) == out:
        return "output folder must not contain the input folder"
    if os.path.isdir(out) and os.listdir(out):
        return f"output folder is not empty: {out}"
    return ""


def execute(plan, out, mask=lambda t: t):
    root = plan["root"]
    err = check_output(root, out)
    if err:
        raise ValueError(err)
    parent = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(parent, exist_ok=True)
    free = shutil.disk_usage(parent).free
    need = int(plan["total_bytes"] * 1.05) + 10 * 2**20
    if free < need:
        raise ValueError(f"not enough space: need {need / 2**20:.0f} MB, free {free / 2**20:.0f} MB")
    dsts = [d for _, d, _ in plan["items"]]
    if len(set(x.lower() for x in dsts)) != len(dsts):
        raise ValueError("internal error: two files map to the same destination - nothing copied")
    os.makedirs(out, exist_ok=True)
    bad = []
    with open(os.path.join(out, "manifest.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["source", "destination", "bytes", "verified"])
        for src_rel, dst_rel, size in plan["items"]:
            src = os.path.join(root, src_rel)
            dst = os.path.join(out, dst_rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                raise ValueError(f"refusing to overwrite existing file {dst_rel}")
            shutil.copy2(src, dst)
            ok = os.path.getsize(dst) == size
            if not ok:
                bad.append(dst_rel)
            w.writerow([src_rel if plan["show_phi"] else mask(src_rel), dst_rel, size, ok])
        for src_rel, kept in plan["duplicates"]:
            w.writerow([src_rel if plan["show_phi"] else mask(src_rel), f"(duplicate of {kept})", "", ""])
    summary = (f"Organized {dt.datetime.now():%Y-%m-%d %H:%M}\n"
               f"source: {root}\n{describe_plan(plan, out, max_lines=10**6)}\n\n"
               f"verified copies: {len(plan['items']) - len(bad)}/{len(plan['items'])}\n"
               "NOTE: files are organized, NOT anonymized. Headers still contain patient data.\n")
    from . import layout as L
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"tool: orin-agent {L.version()}\n" + summary)
    return {"copied": len(plan["items"]), "failed": bad, "duplicates": len(plan["duplicates"]), "out": out}
