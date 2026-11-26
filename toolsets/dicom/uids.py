"""
uids.py - The DICOM UID dictionary (offline, from pydicom) and the category of each SOP class.

pydicom ships every UID of the DICOM standard: SOP classes, transfer syntaxes, well-known frames of
reference, coding schemes ... (482 in pydicom 3.0). The category of a SOP class is derived from its UID
and its name, so all storage classes of the standard get one, also those added in later pydicom versions.
OVERRIDES keeps the short names and categories that were set by hand; they win over the derived ones.
"""
import re

from pydicom._uid_dict import UID_dictionary
from pydicom.uid import UID

STORAGE_ROOT = "1.2.840.10008.5.1.4.1.1."
RT_ROOTS = ("1.2.840.10008.5.1.4.1.1.481.", "1.2.840.10008.5.1.4.34.")

# SOP Class UID -> (short name, category). Hand-set, wins over the derived category.
OVERRIDES = {
    "1.2.840.10008.5.1.4.1.1.2": ("CT Image", "image"),
    "1.2.840.10008.5.1.4.1.1.2.1": ("Enhanced CT", "image"),
    "1.2.840.10008.5.1.4.1.1.4": ("MR Image", "image"),
    "1.2.840.10008.5.1.4.1.1.4.1": ("Enhanced MR", "image"),
    "1.2.840.10008.5.1.4.1.1.128": ("PET Image", "image"),
    "1.2.840.10008.5.1.4.1.1.20": ("NM Image", "image"),
    "1.2.840.10008.5.1.4.1.1.1": ("CR Image", "image"),
    "1.2.840.10008.5.1.4.1.1.1.1": ("DX Image", "image"),
    "1.2.840.10008.5.1.4.1.1.1.2": ("Mammography", "image"),
    "1.2.840.10008.5.1.4.1.1.6.1": ("Ultrasound", "image"),
    "1.2.840.10008.5.1.4.1.1.3.1": ("US Multi-frame", "image"),
    "1.2.840.10008.5.1.4.1.1.12.1": ("X-Ray Angio", "image"),
    "1.2.840.10008.5.1.4.1.1.7": ("Secondary Capture", "secondary_capture"),
    "1.2.840.10008.5.1.4.1.1.7.4": ("SC Multi-frame True Color", "secondary_capture"),
    "1.2.840.10008.5.1.4.1.1.88.67": ("X-Ray Dose SR", "dose_report"),
    "1.2.840.10008.5.1.4.1.1.88.11": ("Basic Text SR", "structured_report"),
    "1.2.840.10008.5.1.4.1.1.88.22": ("Enhanced SR", "structured_report"),
    "1.2.840.10008.5.1.4.1.1.88.33": ("Comprehensive SR", "structured_report"),
    "1.2.840.10008.5.1.4.1.1.104.1": ("Encapsulated PDF", "document"),
    "1.2.840.10008.5.1.4.1.1.11.1": ("Presentation State", "presentation_state"),
    "1.2.840.10008.5.1.4.1.1.481.1": ("RT Image", "rt"),
    "1.2.840.10008.5.1.4.1.1.481.2": ("RT Dose", "rt"),
    "1.2.840.10008.5.1.4.1.1.481.3": ("RT Structure Set", "rt"),
    "1.2.840.10008.5.1.4.1.1.481.5": ("RT Plan", "rt"),
    "1.2.840.10008.5.1.4.1.1.481.8": ("RT Ion Plan", "rt"),
    "1.2.840.10008.5.1.4.1.1.66.1": ("Spatial Registration", "registration"),
    "1.2.840.10008.5.1.4.1.1.66.3": ("Deformable Registration", "registration"),
    "1.2.840.10008.5.1.4.1.1.66.4": ("Segmentation", "segmentation"),
}

CATEGORY_HELP = {
    "image": "image series (a volume or single images)",
    "multiframe_image": "enhanced / legacy-converted multi-frame image: listed, but not exported to MHA yet "
                        "(multi-frame export is not validated)",
    "secondary_capture": "screenshot or scanned image, may contain burned-in text",
    "dose_report": "radiation dose report (SR or screenshot)",
    "structured_report": "structured report or key object selection",
    "document": "encapsulated document (PDF, CDA, STL ...)",
    "presentation_state": "display settings for other images, no pixel data",
    "rt": "radiotherapy object (structure set, plan, dose, RT image, treatment record ...)",
    "registration": "registration between frames of reference",
    "segmentation": "segmentation (DICOM SEG or surface)",
    "other": "anything else (waveforms, raw data, measurements, protocols ...)",
}


def _short(name):
    return re.sub(r" Storage(?= -|$)", "", name).strip()


def derive_category(uid, name):
    """Category of a SOP class from its UID and its standard name (no hand table)."""
    if uid.startswith(RT_ROOTS):
        return "rt"
    n = name
    if "Radiation Dose SR" in n:
        return "dose_report"
    if "Secondary Capture" in n:
        return "secondary_capture"
    if n.startswith("Encapsulated "):
        return "document"
    if "Presentation State" in n:
        return "presentation_state"
    if re.search(r"\bSR Storage|Key Object Selection|Procedure Log", n):
        return "structured_report"
    if "Registration" in n:
        return "registration"
    if "Segmentation Storage" in n:
        return "segmentation"
    if uid.startswith(STORAGE_ROOT) and re.search(r"Image Storage|Volume Storage", n):
        if n.startswith(("Enhanced ", "Legacy Converted ")):
            return "multiframe_image"
        return "image"
    return "other"


def sop_category(uid):
    """(short name, category) for a SOP Class UID. Unknown UIDs: (name or 'unknown', 'other')."""
    uid = str(uid or "").strip()
    if uid in OVERRIDES:
        return OVERRIDES[uid]
    e = UID_dictionary.get(uid)
    if not e:
        return ("unknown" if not uid else f"unknown SOP class {uid}"), "other"
    return _short(e[0]), (derive_category(uid, e[0]) if e[1] == "SOP Class" else "other")


def _entry(uid):
    name, kind, info, retired, keyword = UID_dictionary[uid]
    d = {"uid": uid, "name": name, "type": kind}
    if keyword:
        d["keyword"] = keyword
    if retired:
        d["retired"] = True
    if info:
        d["info"] = info
    if kind == "SOP Class" and ("Storage" in name or uid in OVERRIDES):
        cat = sop_category(uid)[1]
        d["category"] = cat
        d["category_means"] = CATEGORY_HELP.get(cat, "")
    if kind == "Transfer Syntax":
        try:
            d["compressed"] = bool(UID(uid).is_compressed)
        except Exception:
            pass
        low = name.lower()
        if "lossless" in low:
            d["lossy"] = False
        elif any(w in low for w in ("lossy", "baseline", "extended", "near-lossless", "mpeg", "hevc")):
            d["lossy"] = True
    return d


UID_RE = re.compile(r"^[0-9]+(\.[0-9]+)+$")


def lookup(query, limit=25):
    """A UID -> its entry. Words -> all entries whose name or keyword contains every word."""
    q = str(query or "").strip()
    if not q:
        return {"error": "give a UID (e.g. 1.2.840.10008.5.1.4.1.1.481.3) or words (e.g. 'RT plan', 'JPEG')"}
    if UID_RE.match(q):
        if q in UID_dictionary:
            return _entry(q)
        d = {"uid": q, "found": False,
             "note": "not a UID of the DICOM standard. If it came from StudyInstanceUID / SeriesInstanceUID / "
                     "SOPInstanceUID / FrameOfReferenceUID it is an instance UID made by the scanner or planning "
                     "system: it only identifies one object and has no meaning to look up."}
        if q.startswith("1.2.840.10008."):
            d["note"] = "uses the DICOM root 1.2.840.10008 but is not in the dictionary of this pydicom version " \
                        "(newer standard, or wrong / truncated UID)."
        elif not q.startswith("1.2.840.10008"):
            d["root"] = ".".join(q.split(".")[:4]) + "..."
        return d
    words = [w.lower() for w in re.split(r"\s+", q) if w]
    hits = [u for u, e in UID_dictionary.items()
            if all(w in (e[0] + " " + e[4] + " " + e[1]).lower() for w in words)]
    hits.sort(key=lambda u: (UID_dictionary[u][3] != "", UID_dictionary[u][1], UID_dictionary[u][0]))
    out = [_entry(u) for u in hits[:limit]]
    res = {"query": q, "matches": len(hits), "results": out}
    if len(hits) > limit:
        res["note"] = f"showing {limit} of {len(hits)}, use more words to narrow it"
    if not hits:
        res["note"] = "no UID name contains all these words; try fewer or English words (e.g. 'structure set')"
    return res
