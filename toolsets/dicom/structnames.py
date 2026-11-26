"""Structure (ROI) name standardization to AAPM TG-263.

    standardize(name, mapping=None) -> {"original", "standard", "status", "note"}

status:
  mapping    taken from the user's mapping file (always wins)
  standard   already a TG-263 name
  synonym    known synonym / abbreviation / German name (+ laterality, PRV)
  target     GTV/CTV/ITV/PTV, rebuilt as Type[classifier]_[dose cGy] - please review
  point      isocenter / reference point / marker: name kept
  helper     non-clinical structure (ring, opt, avoid, ...): TG-263 'z' prefix
  unmapped   not recognised: the original name is kept (add it to the mapping file)

Deterministic, no model involved. The mapping file is a CSV with columns `original,standard`
(lines starting with # are comments); matching ignores case, spaces, '_', '-' and umlauts.
"""
import csv
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_MAPPING = os.path.join(REPO, "configs", "structure_names.csv")

# TG-263 base name -> (paired organ?, synonyms). Paired organs get _L / _R.
# Synonyms are written as they are typed; they are normalized below.
ORGANS = {
    # CNS / head & neck
    "Brain": (False, ["brain", "gehirn", "hirn", "cerebrum"]),
    "Brainstem": (False, ["brainstem", "brain stem", "hirnstamm", "truncus", "stammhirn", "bs"]),
    "SpinalCord": (False, ["spinal cord", "spinalcord", "cord", "myelon", "rueckenmark", "rm", "medulla spinalis",
                           "sc", "myelon cervical", "halsmark", "brustmark"]),
    "SpinalCanal": (False, ["spinal canal", "spinalcanal", "spinalkanal", "wirbelkanal", "canalis spinalis"]),
    "CaudaEquina": (False, ["cauda equina", "cauda"]),
    "OpticChiasm": (False, ["optic chiasm", "chiasm", "chiasma", "chiasma opticum", "chiasmaopticum",
                            "sehnervenkreuzung"]),
    "OpticNrv": (True, ["optic nerve", "opticnerve", "optic nrv", "n opticus", "nervus opticus", "sehnerv", "on",
                        "optikus", "opticus", "n optikus"]),
    "Eye": (True, ["eye", "globe", "auge", "bulbus", "bulbus oculi", "augapfel", "orbita"]),
    "Lens": (True, ["lens", "linse", "augenlinse"]),
    "Retina": (True, ["retina", "netzhaut"]),
    "Cornea": (True, ["cornea", "hornhaut", "kornea"]),
    "Glnd_Lacrimal": (True, ["lacrimal gland", "traenendruese", "glandula lacrimalis"]),
    "Cochlea": (True, ["cochlea", "innenohr", "inner ear", "kochlea", "hoerschnecke", "schnecke"]),
    "Pituitary": (False, ["pituitary", "hypophyse", "hypophysis"]),
    "Hippocampus": (True, ["hippocampus", "hippo", "hippokampus"]),
    "Lobe_Temporal": (True, ["temporal lobe", "temporallappen", "lobe temporal", "schlaefenlappen"]),
    "Parotid": (True, ["parotid", "parotid gland", "parotis", "glandula parotis", "ohrspeicheldruese",
                       "gl parotis", "parotide"]),
    "Glnd_Submand": (True, ["submandibular", "submandibular gland", "submand", "glandula submandibularis",
                            "unterkieferspeicheldruese", "smg", "submandibularis", "gl submandibularis"]),
    "Bone_Mandible": (False, ["mandible", "mandibula", "unterkiefer"]),
    "Cavity_Oral": (False, ["oral cavity", "cavity oral", "mundhoehle", "oralcavity", "mundraum"]),
    "Tongue": (False, ["tongue", "zunge"]),
    "Lips": (False, ["lips", "lippen", "lip"]),
    "Pharynx": (False, ["pharynx", "rachen"]),
    "Larynx": (False, ["larynx", "kehlkopf"]),
    "Glottis": (False, ["glottis", "stimmritze", "stimmlippen"]),
    "Musc_Constrict": (False, ["constrictors", "pharyngeal constrictors", "constrictor", "pcm", "konstriktoren",
                               "schlundschnuerer", "pharynxkonstriktoren"]),
    "Esophagus": (False, ["esophagus", "oesophagus", "speiseroehre", "osophagus", "oeso", "ösophagus"]),
    "Thyroid": (False, ["thyroid", "schilddruese", "thyreoidea", "glandula thyroidea"]),
    "Trachea": (False, ["trachea", "luftroehre"]),
    "BrachialPlex": (True, ["brachial plexus", "brachialplexus", "plexus brachialis", "plexus", "armplexus",
                            "armnervengeflecht"]),
    "A_Carotid": (True, ["carotid", "carotis", "karotis", "a carotis", "arteria carotis", "halsschlagader"]),
    # thorax
    "Lung": (True, ["lung", "lunge", "pulmo", "lungenfluegel"]),
    "Lungs": (False, ["lungs", "lungen", "both lungs", "lunge gesamt", "lungs total", "lung total",
                      "lunge bds", "lung both", "total lung", "lunge beidseits", "lungen beidseits", "pulmones"]),
    "Heart": (False, ["heart", "herz", "cor"]),
    "Pericardium": (False, ["pericardium", "perikard", "herzbeutel"]),
    "Breast": (True, ["breast", "brust", "mamma", "brustdruese"]),
    "Chestwall": (False, ["chestwall", "chest wall", "brustwand", "thoraxwand"]),
    "A_Aorta": (False, ["aorta", "hauptschlagader"]),
    "A_LAD": (False, ["lad", "ria", "ramus interventricularis anterior"]),
    "V_Venacava_S": (False, ["superior vena cava", "vena cava superior", "vcs", "obere hohlvene", "svc"]),
    "V_Venacava_I": (False, ["inferior vena cava", "vena cava inferior", "vci", "untere hohlvene", "ivc"]),
    "Bronchus": (False, ["bronchus", "bronchial tree", "bronchialbaum", "proximal bronchial tree", "pbt",
                         "zentrale atemwege", "central airways", "bronchien", "hauptbronchien"]),
    "GreatVes": (False, ["great vessels", "grosse gefaesse", "gefaesse gross"]),
    # abdomen / pelvis
    "Liver": (False, ["liver", "leber", "hepar"]),
    "Gallbladder": (False, ["gallbladder", "gallenblase", "vesica fellea"]),
    "Stomach": (False, ["stomach", "magen"]),
    "Duodenum": (False, ["duodenum", "zwoelffingerdarm"]),
    "Pancreas": (False, ["pancreas", "pankreas", "bauchspeicheldruese"]),
    "Spleen": (False, ["spleen", "milz", "lien"]),
    "Adrenal": (True, ["adrenal", "adrenal gland", "nebenniere"]),
    "Kidney": (True, ["kidney", "niere", "ren"]),
    "Kidneys": (False, ["kidneys", "nieren", "both kidneys", "niere bds", "nieren bds", "nieren beidseits"]),
    "Bowel": (False, ["bowel", "darm", "bowel bag", "bowelbag", "darmkonvolut", "intestine", "darmschlingen",
                      "darmsack"]),
    "Bowel_Small": (False, ["small bowel", "smallbowel", "duenndarm", "small intestine", "duenndarmschlingen"]),
    "Bowel_Large": (False, ["large bowel", "largebowel", "dickdarm", "colon", "kolon"]),
    "Colon_Sigmoid": (False, ["sigmoid", "sigma", "colon sigmoid", "sigmoid colon", "sigmoid kolon"]),
    "Rectum": (False, ["rectum", "rektum", "enddarm", "mastdarm"]),
    "Anus": (False, ["anus", "analkanal", "anal canal", "schliessmuskel", "sphinkter"]),
    "Bladder": (False, ["bladder", "blase", "harnblase", "urinary bladder", "vesica", "vesica urinaria"]),
    "Prostate": (False, ["prostate", "prostata", "vorsteherdruese"]),
    "SeminalVes": (False, ["seminal vesicles", "seminal vesicle", "samenblasen", "samenblase", "sv", "sbl",
                           "vesiculae seminales", "samenblaeschen"]),
    "PenileBulb": (False, ["penile bulb", "bulbus penis", "penilebulb", "bulb", "peniswurzel", "penisbulbus"]),
    "Urethra": (False, ["urethra", "harnroehre"]),
    "Uterus": (False, ["uterus", "gebaermutter"]),
    "Cervix": (False, ["cervix", "zervix", "gebaermutterhals", "cervix uteri"]),
    "Vagina": (False, ["vagina", "scheide"]),
    "Ovary": (True, ["ovary", "ovar", "eierstock"]),
    "Testis": (True, ["testis", "hoden", "testicle"]),
    "Femur_Head": (True, ["femoral head", "femur head", "femurkopf", "hueftkopf", "caput femoris", "femur",
                          "oberschenkelkopf", "femurkoepfe", "hueftkoepfe"]),
    "Bone_Pelvic": (False, ["pelvis", "becken", "pelvic bone", "beckenknochen"]),
    "Cavity_Abdominal": (False, ["abdomen", "peritoneal cavity", "bauchraum", "bauchhoehle"]),
    # whole body / devices
    "External": (False, ["external", "body", "koerper", "aussenkontur", "skin contour", "outline", "patient",
                         "body contour", "koerperkontur", "haut aussen", "boundary",   # boundary: ViewRay outline
                         "patientenkontur", "koerperumriss"]),
    "Skin": (False, ["skin", "haut"]),
    "CouchSurface": (False, ["couch surface", "couchsurface", "tischoberflaeche", "couch outer", "tisch aussen"]),
    "CouchInterior": (False, ["couch interior", "couchinterior", "couch inner", "tisch innen"]),
}

# Organ minus target, e.g. "Lunge-GTV", "Lungen ohne PTV", "Leber minus GTV" -> TG-263 "Lungs-GTV"
MINUS_WORDS = {"minus", "ohne", "abzgl", "exkl", "excl", "exclusive"}
# German target descriptors -> TG-263 classifier
PRIMARY_WORDS = {"primarius", "primaer", "primaertumor", "primary", "prim", "tumor", "pt"}
NODAL_WORDS = {"ln", "lk", "lnn", "node", "nodes", "nodal", "lymphknoten", "law", "lymphabfluss",
               "lymphabflusswege", "lymphabflussgebiet"}

LEFT = {"l", "lt", "left", "li", "links", "lks", "sin", "sinister", "sinistra", "lft", "linke", "linker", "linkes"}
RIGHT = {"r", "rt", "right", "re", "rechts", "dex", "dexter", "dextra", "rgt", "rts", "rechte", "rechter",
         "rechtes"}
BOTH = {"bds", "both", "beide", "bilateral", "bilat", "total", "gesamt", "combined", "all", "beidseits", "bdsts",
        "ges"}
# helper prefixes, checked on the original spelling: TG-263 "zPTV_opt" / "z_..." (not "ZENTRALEATEMWEGE"),
# "_...", "x_..." and the German "H_..." (Hilfsstruktur)
SUFFIX_MARKERS = {"p", "new", "old", "orig", "copy", "kopie", "bak", "neu", "alt", "ct", "mr", "plan", "final"}
HELPER_PREFIX = re.compile(r"^(?:z(?=[A-Z0-9])|[zZxXhH]_|_)")
HELPER = re.compile(r"\b(ring|opt|optim\w*|help\w*|hilf\w*|avoid\w*|dummy|tmp|temp|kontrast|contrast|"
                    r"artefact|artifact|artefakt|marker|clip|fiducial|bolus|ptv ?-|minus|plus|norm|block|"
                    # German: air/water density overrides, isodose and dose-shaping structures
                    r"luft|wasser|dichte\w*|override|isodose\w*|dosis\w*|hotspot|coldspot|schutz\w*|"
                    r"ausschluss\w*|expand\w*|erweiter\w*)\b", re.I)
POINT = re.compile(r"^(iso|isoc\w*|isozentrum|ref\w*|point|punkt|poi|marker|bb)\b", re.I)
TARGET = re.compile(r"^(gtv|ctv|itv|ptv|irtv)([a-z]{0,2})\b(.*)$")

_TRANS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def _words(name):
    s = name.translate(_TRANS).lower()
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)          # "prv5" -> "prv 5", "ptv60" -> "ptv 60"
    return re.findall(r"[a-z]+|\d+(?:[.,]\d+)?", s)


def key(name):
    """Matching key: lower case, umlauts transliterated, separators removed."""
    return "".join(re.findall(r"[a-z0-9]+", name.translate(_TRANS).lower()))


SYNONYMS = {}
for _std, (_paired, _syns) in ORGANS.items():
    for _s in _syns + [_std]:
        SYNONYMS.setdefault(key(_s), (_std, _paired))
PAIRED = {std for std, (paired, _) in ORGANS.items() if paired}
STANDARD_NAMES = {s for s in ORGANS} | {f"{s}_{side}" for s in PAIRED for side in "LR"}
STANDARD_KEYS = {n.lower(): n for n in STANDARD_NAMES}
VALID = re.compile(r"^[A-Za-z0-9_^-]{1,64}$")          # "-" for TG-263 "Lungs-GTV"


def load_mapping(path=None):
    """{key(original): standard}. Missing file = empty mapping. Bad rows are reported, not used."""
    path = path or DEFAULT_MAPPING
    out, problems = {}, []
    if not path or not os.path.isfile(path):
        return out, problems
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for n, row in enumerate(csv.reader(fh), 1):
            if not row or row[0].lstrip().startswith("#") or [c.strip().lower() for c in row[:2]] == ["original", "standard"]:
                continue
            if len(row) < 2 or not row[1].strip():
                continue                                   # template line not filled in yet
            orig, std = row[0].strip(), row[1].strip()
            if not VALID.match(std):
                problems.append(f"line {n}: '{std}' is not a valid name (letters, digits, _ only)")
                continue
            out[key(orig)] = std
    return out, problems


def _dose_cgy(tok):
    v = float(tok.replace(",", "."))
    if v <= 0:
        return None
    return int(round(v * 100)) if v <= 120 else int(round(v))   # 60 / 50.4 Gy -> 6000 / 5040; 6000 cGy stays


def _target(words):
    m = TARGET.match(" ".join(words))
    if not m:
        return None
    typ, cls, rest = m.group(1).upper(), m.group(2), m.group(3).split()
    if cls and cls not in ("p", "n", "sb", "m", "vb"):       # "ptvboost" etc.: not a classifier
        rest, cls = [cls] + rest, ""
    if not cls and rest and rest[0] in ("p", "n", "sb", "m", "vb"):   # "CTV_n 54"
        cls, rest = rest[0], rest[1:]
    level_ok = True
    if not cls and any(w in NODAL_WORDS for w in rest):     # "GTV_LN_2R_4R" -> GTVn_2R4R (2R = station)
        cls, rest, level_ok = "n", [w for w in rest if w not in NODAL_WORDS], False
    elif not cls and any(w in PRIMARY_WORDS for w in rest):  # "GTV_Primarius" -> GTVp
        cls, rest = "p", [w for w in rest if w not in PRIMARY_WORDS]
    dose, extra = None, []
    for i, w in enumerate(rest):
        if dose is None and re.fullmatch(r"\d+(?:[.,]\d+)?", w):
            unit = rest[i + 1] if i + 1 < len(rest) else ""
            v = float(w.replace(",", "."))
            if unit not in ("gy", "cgy") and v < 10:
                if i == 0 and level_ok:                      # "PTV1", "PTV 2": a level number, not a dose
                    cls += w
                else:                                        # "LB5", "2R 4R": part of the description
                    extra.append(w)
                continue
            dose = int(round(v)) if unit == "cgy" else _dose_cgy(w)
        elif w in ("gy", "cgy"):
            continue
        else:
            extra.append(w.capitalize())
    name = typ + cls
    if dose:
        name += f"_{dose}"
    if extra:
        name += "_" + "".join(extra)[:16]
    return name


def _organ_minus(words):
    """"Lunge-GTV", "Lungen ohne PTV", "Leber_minus_GTV" -> "Lungs-GTV" / "Liver-GTV"; None if not that form."""
    j = next((i for i, w in enumerate(words) if TARGET.match(w)), None)
    if not j:
        return None
    organ = [w for w in words[:j] if w not in MINUS_WORDS]
    tgt = _target(words[j:])
    if not organ or not tgt:
        return None
    r = standardize(" ".join(organ))
    std = r["standard"] if r["status"] in ("synonym", "standard") else None
    if not std:                                       # "Lunge-GTV" without a side means both lungs
        hit = SYNONYMS.get("".join(organ))
        if hit and hit[1] and hit[0] + "s" in ORGANS:
            std = hit[0] + "s"
    return f"{std}-{tgt.split('_')[0]}" if std else None


def standardize(name, mapping=None):
    res = {"original": name, "standard": name, "status": "unmapped", "note": ""}
    k = key(name)
    if mapping and k in mapping:
        res.update(standard=mapping[k], status="mapping")
        return res
    if name in STANDARD_NAMES:
        res.update(status="standard")
        return res
    if k in STANDARD_KEYS and len(k) > 3:              # right name, wrong case: "spinalcord"
        res.update(standard=STANDARD_KEYS[k], status="synonym")
        return res
    words = _words(name)
    if not words:
        return res
    if POINT.match(" ".join(words)):
        res.update(status="point", note="point / marker: name kept")
        return res
    tgt = _target(words)
    if tgt and HELPER.search(" ".join(words[1:])):        # "PTV-Ring", "PTV_opt": dose-shaping helper
        tgt = None
        safe = re.sub(r"[^A-Za-z0-9]+", "_", name.translate(_TRANS)).strip("_")[:15]
        res.update(standard="z" + safe, status="helper")
        return res
    if tgt:
        res.update(standard=tgt, status="target", note="target: check type/dose")
        return res
    explicit_helper = HELPER_PREFIX.match(name.translate(_TRANS))
    if not explicit_helper:
        om = _organ_minus(words)
        if om:
            res.update(standard=om, status="synonym", note="organ minus target")
            return res
    if explicit_helper or HELPER.search(name.translate(_TRANS).lower()):
        base = re.sub(r"^(?:[xXhH]_|_)", "", name.translate(_TRANS))   # "H_RING" -> "zRING"
        safe = re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_")[:15]
        res.update(standard=safe if safe.lower().startswith("z") else "z" + safe, status="helper")
        return res

    side, prv, both, base = None, None, False, []
    i = 0
    while i < len(words):
        w = words[i]
        if w in LEFT:
            side = "L" if side in (None, "L") else "?"
        elif w in RIGHT:
            side = "R" if side in (None, "R") else "?"
        elif w in BOTH:
            both = True
        elif w == "prv":
            prv = 0
            if i + 1 < len(words) and re.fullmatch(r"\d+", words[i + 1]):
                prv = int(words[i + 1])
                i += 1
            if i + 1 < len(words) and words[i + 1] == "mm":
                i += 1
        elif w in ("mm",) and prv is not None:
            pass
        elif prv is None and base and re.fullmatch(r"\d+", w) and i + 1 < len(words) and words[i + 1] == "mm":
            prv = int(w)                                   # "Myelon+5mm", "RM 3 mm" -> PRV
            i += 1
        else:
            base.append(w)
        i += 1
    # site / version markers at the end ("Heart_P", "Lung_new", "Rectum_old", "Bladder copy") are not part of the
    # organ name: ignored, and said so in the note
    ignored = []
    while len(base) > 1 and base[-1] in SUFFIX_MARKERS and not SYNONYMS.get("".join(base)):
        ignored.insert(0, base.pop())
    # glued laterality: "parotisli", "lungl", "lunge_re" already split; try trailing l/r/li/re on the last word
    hit = SYNONYMS.get("".join(base))
    if not hit and base and side is None:
        last = base[-1]
        for suf, s in (("li", "L"), ("re", "R"), ("lt", "L"), ("rt", "R"), ("l", "L"), ("r", "R")):
            if last.endswith(suf) and SYNONYMS.get("".join(base[:-1]) + last[: -len(suf)]):
                hit, side = SYNONYMS["".join(base[:-1]) + last[: -len(suf)]], s
                break
    if not hit:
        return res
    std, paired = hit
    if side == "?":
        res["note"] = "both left and right in the name"
        return res
    if paired:
        if side:
            std = f"{std}_{side}"
        elif both and std + "s" in ORGANS:
            std = std + "s"
        else:
            res["note"] = f"looks like {std} but side (L/R) unknown"
            return res
    elif side:
        res["note"] = f"looks like {std} but has a side - not a paired organ"
        return res
    if prv is not None:
        std += f"_PRV{prv:02d}" if prv else "_PRV"
    res.update(standard=std, status="synonym",
               note=f"suffix '{'_'.join(ignored)}' ignored" if ignored else res["note"])
    return res


def standardize_all(names, mapping=None):
    """Standardize a list of names; duplicates of the same standard name get _2, _3 and a note."""
    out, seen = [], {}
    for n in names:
        r = standardize(n, mapping)
        base = r["standard"]
        if base in seen:
            seen[base] += 1
            r["standard"] = f"{base}_{seen[base]}"
            r["note"] = (r["note"] + "; " if r["note"] else "") + f"duplicate of {base}"
        else:
            seen[base] = 1
        out.append(r)
    return out


def rtstruct_roi_names(path):
    """ROI names of an RTSTRUCT file, in ROINumber order."""
    import pydicom
    ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
    rois = [(int(getattr(r, "ROINumber", 0) or 0), str(getattr(r, "ROIName", "") or ""))
            for r in getattr(ds, "StructureSetROISequence", [])]
    return [n for _, n in sorted(rois)]


def write_template(rows, path):
    """CSV with the names that were not mapped, for the user to fill in."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["original", "standard"])
        for r in rows:
            w.writerow([r["original"], ""])
