#!/usr/bin/env python3
"""
Regression tests (synthetic data only - no patient data). Run after every code change:

    ~/dicom-env/bin/python tests/run_tests.py            # exit code 0 = all passed

1. anonymization: identifiers planted in many places (tags, private tags, nested sequences, free text,
   RTSTRUCT labels) must not appear ANYWHERE in the output files (raw byte search), RTSTRUCT -> CT references
   must still match, dates shifted with intervals kept, no private tags left
2. export check: finds the identifiers in the original, finds none in the anonymized output, checks names
3. folder layout (case/original -> case/<step>)
4. structure tools: union / intersection / minus exact, margins against analytic spheres, label parsing
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import traceback

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, RTStructureSetStorage, generate_uid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PHI = {"name": "MUSTERMANN^ERIKA", "id": "PID48213", "birth": "19650312", "acc": "ACC778899",
       "physician": "DOKTOR^HEINRICH", "serial": "SN-445566", "institution": "Klinik Sonnenhof"}
# what must never appear in any anonymized output file (lower case, raw bytes)
FORBIDDEN = ["mustermann", "erika", "pid48213", "19650312", "1965-03-12", "acc778899", "doktor", "heinrich",
             "sn-445566", "sonnenhof"]
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


# ------------------------------------------------------------------ synthetic data with planted identifiers
def _file(path, sop_class):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(path, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = sop_class, meta.MediaStorageSOPInstanceUID
    return ds


def _patient(ds, study, for_uid, modality):
    ds.PatientName, ds.PatientID, ds.PatientBirthDate = PHI["name"], PHI["id"], PHI["birth"]
    ds.PatientSex, ds.OtherPatientIDs = "F", PHI["id"] + "-X"
    ds.StudyInstanceUID, ds.StudyDate, ds.StudyTime, ds.StudyID = study, "20240115", "101500", "1"
    ds.AccessionNumber, ds.ReferringPhysicianName = PHI["acc"], PHI["physician"]
    ds.InstitutionName, ds.InstitutionAddress = PHI["institution"], "Hauptstrasse 1, Sonnenhof"
    # staff name deliberately uncommon: dicognito invents common names ("ANNA"), and the verification then
    # flags the invented name as the original one (a harmless false alarm that made this test flaky)
    ds.OperatorsName, ds.PerformingPhysicianName = "TECHNIKER^ZYPRIANA", PHI["physician"]
    ds.DeviceSerialNumber, ds.StationName = PHI["serial"], "CT-SONNENHOF-1"
    ds.FrameOfReferenceUID, ds.Modality = for_uid, modality
    ds.StudyDescription = "Thorax Erika Mustermann"
    # nested sequence with identifiers
    item = Dataset()
    item.RequestedProcedureDescription = f"CT for {PHI['name']}"
    item.RequestingPhysician = PHI["physician"]
    item.ScheduledProcedureStepID = PHI["acc"]
    ds.RequestAttributesSequence = [item]
    # private tags with identifiers
    ds.add_new(0x00110010, "LO", "ACME private")
    ds.add_new(0x00111001, "LO", f"{PHI['name']} {PHI['id']}")


def make_phi_dataset(out):
    os.makedirs(out)
    study, for_uid, series = generate_uid(), generate_uid(), generate_uid()
    sops = []
    for k in range(4):
        ds = _file(os.path.join(out, f"Mustermann_CT{k}.dcm"), CTImageStorage)
        _patient(ds, study, for_uid, "CT")
        ds.SeriesInstanceUID, ds.SeriesNumber, ds.InstanceNumber = series, 2, k + 1
        ds.SeriesDescription = "CT Mustermann planning"
        ds.ImageComments = f"pat {PHI['id']} born 1965-03-12"
        ds.SeriesDate = ds.ContentDate = ds.AcquisitionDate = "20240115"
        ds.ImagePositionPatient, ds.ImageOrientationPatient = [0, 0, float(k * 3)], [1, 0, 0, 0, 1, 0]
        ds.PixelSpacing, ds.SliceThickness, ds.Rows, ds.Columns = [1, 1], 3, 16, 16
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 1
        ds.RescaleIntercept, ds.RescaleSlope = 0, 1
        ds.PixelData = (np.arange(256, dtype=np.int16).reshape(16, 16) % 50).tobytes()
        ds.save_as(ds.filename, enforce_file_format=True)
        sops.append(ds.SOPInstanceUID)
    rs = _file(os.path.join(out, "RS_Mustermann.dcm"), RTStructureSetStorage)
    _patient(rs, study, for_uid, "RTSTRUCT")
    rs.SeriesInstanceUID, rs.SeriesNumber = generate_uid(), 9
    rs.SeriesDescription, rs.StructureSetLabel = "RT Mustermann", "Plan Mustermann"
    rs.StructureSetName, rs.StructureSetDate, rs.StructureSetTime = f"for {PHI['id']}", "20240116", "090000"
    rf = Dataset()
    rf.FrameOfReferenceUID = for_uid
    st = Dataset()
    st.ReferencedSOPClassUID, st.ReferencedSOPInstanceUID = "1.2.840.10008.3.1.2.3.1", study
    se = Dataset()
    se.SeriesInstanceUID = series
    se.ContourImageSequence = []
    for s in sops:
        ci = Dataset()
        ci.ReferencedSOPClassUID, ci.ReferencedSOPInstanceUID = CTImageStorage, s
        se.ContourImageSequence.append(ci)
    st.RTReferencedSeriesSequence = [se]
    rf.RTReferencedStudySequence = [st]
    rs.ReferencedFrameOfReferenceSequence = [rf]
    roi = Dataset()
    roi.ROINumber, roi.ReferencedFrameOfReferenceUID, roi.ROIName = 1, for_uid, "GTV"
    roi.ROIDescription = "drawn by Dr Heinrich"
    rs.StructureSetROISequence = [roi]
    rc = Dataset()
    rc.ReferencedROINumber, rc.ROIDisplayColor = 1, [255, 0, 0]
    c = Dataset()
    c.ContourGeometricType, c.NumberOfContourPoints = "CLOSED_PLANAR", 4
    c.ContourData = [2, 2, 3, 10, 2, 3, 10, 10, 3, 2, 10, 3]
    ci = Dataset()
    ci.ReferencedSOPClassUID, ci.ReferencedSOPInstanceUID = CTImageStorage, sops[1]
    c.ContourImageSequence = [ci]
    rc.ContourSequence = [c]
    rs.ROIContourSequence = [rc]
    obs = Dataset()
    obs.ObservationNumber, obs.ReferencedROINumber, obs.RTROIInterpretedType = 1, 1, "GTV"
    obs.ROIInterpreter = PHI["physician"]
    rs.RTROIObservationsSequence = [obs]
    rs.save_as(rs.filename, enforce_file_format=True)
    return series


# ------------------------------------------------------------------ tests
def test_anonymization(tmp):
    from toolsets.dicom import anonymize as A
    from toolsets.dicom import export_check as X
    from toolsets.dicom.common import Scanned
    src = os.path.join(tmp, "phi_case", "original")
    ct_series = make_phi_dataset(src)
    sc = Scanned(src)
    out = os.path.join(tmp, "phi_case", "anonymized")
    r = A.run(sc.root, out, sc.res, sc.tree, sc.rows, sc.files)
    check("anonymize: all files written", r["written"] == 5 and not r["failed"], f"{r['written']} written")
    check("anonymize: own verification clean", not r["problems"], f"{r['problems'][:3]}")
    files = [os.path.join(d, f) for d, _, fs in os.walk(out) for f in fs]
    leaks = []
    for f in files:
        data = open(f, "rb").read().lower()
        leaks += [f"{os.path.basename(f)}:{t}" for t in FORBIDDEN if t.encode() in data]
        leaks += [f"name:{t}" for t in FORBIDDEN if t in os.path.relpath(f, out).lower()]
    check("anonymize: no planted identifier anywhere in output bytes or file names", not leaks,
          ", ".join(sorted(set(leaks))[:8]))
    dcm = [pydicom.dcmread(f) for f in files if f.endswith(".dcm")]
    check("anonymize: PatientName = ANON^...", all(str(d.PatientName).startswith("ANON^") for d in dcm))
    check("anonymize: no private tags", not any(el.tag.is_private for d in dcm for el in d.iterall()))
    check("anonymize: DeidentificationMethod has tool version",
          all("orin-agent" in str(d.get("DeidentificationMethod", "")) for d in dcm))
    cts = [d for d in dcm if d.Modality == "CT"]
    rs = [d for d in dcm if d.Modality == "RTSTRUCT"][0]
    ref = rs.ReferencedFrameOfReferenceSequence[0].RTReferencedStudySequence[0].RTReferencedSeriesSequence[0]
    check("anonymize: RTSTRUCT still references the (new) CT series UID",
          ref.SeriesInstanceUID == cts[0].SeriesInstanceUID and ref.SeriesInstanceUID != ct_series)
    ct_sops = {d.SOPInstanceUID for d in cts}
    check("anonymize: contour image references point to the new CT SOP UIDs",
          all(ci.ReferencedSOPInstanceUID in ct_sops for ci in ref.ContourImageSequence))
    # dicognito shifts date AND time, so compare full timestamps: 15 Jan 10:15 -> 16 Jan 09:00 = 22 h 45 min
    # (dates alone may differ by 0 or 1 day once the shifted time crosses midnight)
    d0 = dt.datetime.strptime(cts[0].StudyDate + str(cts[0].StudyTime)[:6], "%Y%m%d%H%M%S")
    d1 = dt.datetime.strptime(rs.StructureSetDate + str(rs.StructureSetTime)[:6], "%Y%m%d%H%M%S")
    check("anonymize: dates shifted, date+time interval kept (22 h 45 min)",
          cts[0].StudyDate != "20240115" and d1 - d0 == dt.timedelta(hours=22, minutes=45),
          f"{d0} / {d1}")
    ids = X.identifiers(src)
    check("export check: identifiers read from original",
          "mustermann" in ids["names"] and PHI["id"].lower() in ids["ids"] and "1965-03-12" in ids["dates"])
    check("export check: finds identifiers in original", len(X.scan(src, ids)["hits"]) >= 5)
    ra = X.scan(out, ids)
    check("export check: anonymized output clean", not ra["hits"], X.summary(ra))
    check("export check: case-name check", X.scan_name("Mustermann_2024", ids) and not X.scan_name("case-01", ids))
    p = os.path.join(tmp, "leak.json")
    open(p, "w").write('{"note": "patient Erika"}')
    check("export check: finds a name in a text file", X.scan(tmp, ids)["hits"] and
          any(h[0] == "leak.json" for h in X.scan(tmp, ids)["hits"]))


def test_layout(tmp):
    from toolsets.dicom import layout as L
    case = os.path.join(tmp, "phi_case")
    check("layout: case folder -> original", L.input_dir(case) == os.path.join(case, "original"))
    check("layout: original -> case/mha", L.out(os.path.join(case, "original"), "mha") == os.path.join(case, "mha"))
    other = os.path.join(tmp, "plain")
    os.makedirs(other)
    check("layout: non-case folder keeps <folder>_mha", L.out(other, "mha") == other + "_mha")
    check("layout: version string", L.version() not in ("", "unknown"), L.version())
    check("layout: temp files outside the vault stay default", L.use_session_tmp(other) == tempfile.gettempdir())


def _sphere(r, size=64, sp=1.0, center=None):
    import SimpleITK as sitk
    c = np.array(center if center is not None else [size / 2] * 3)
    z, y, x = np.indices((size, size, size)) * sp
    a = (((x - c[0]) ** 2 + (y - c[1]) ** 2 + (z - c[2]) ** 2) <= r * r).astype(np.uint8)
    img = sitk.GetImageFromArray(a)
    img.SetSpacing((sp, sp, sp))
    return img


def test_structures():
    import SimpleITK as sitk
    from toolsets.dicom import mha_tools as T
    from toolsets.dicom.structure_ops import parse_labels
    a, b = _sphere(10, center=(28, 32, 32)), _sphere(10, center=(36, 32, 32))
    na, nb = (sitk.GetArrayViewFromImage(x).sum() for x in (a, b))
    inter = int((sitk.GetArrayViewFromImage(a) & sitk.GetArrayViewFromImage(b)).sum())
    u = sitk.GetArrayViewFromImage(T.combine([a, b])[0]).sum()
    i = sitk.GetArrayViewFromImage(T.combine([a, b], "intersection")[0]).sum()
    m = sitk.GetArrayViewFromImage(T.combine([a, b], "minus")[0]).sum()
    check("structures: union / intersection / minus exact", u == na + nb - inter and i == inter and m == na - inter)
    r10, r15 = _sphere(10), _sphere(15)
    # 1 mm voxels: a voxelised sphere differs from the true one by up to ~1 voxel at the surface
    grow = T.compare(T.margin(r10, 5), r15)
    check("structures: sphere r10 + 5 mm vs r15: Dice > 0.95, mean surface dist < 0.5 mm",
          grow["dice"] > 0.95 and grow["mean_surface_distance_mm"] < 0.5, str(grow))
    shrink = T.compare(T.margin(r15, -5), r10)
    check("structures: sphere r15 - 5 mm vs r10: Dice > 0.90, mean surface dist < 0.5 mm",
          shrink["dice"] > 0.90 and shrink["mean_surface_distance_mm"] < 0.5, str(shrink))
    aniso = T.structure_stats(T.margin(r10, (0, 0, 4)))
    check("structures: margin 0,0,4 grows z only", aniso["extent_mm"][2] - aniso["extent_mm"][0] in (8.0, 9.0, 10.0),
          str(aniso["extent_mm"]))
    lab, table, _ = T.labelmap([("A", a, 1), ("B", b, 2)])
    check("structures: label map values + table", set(np.unique(sitk.GetArrayViewFromImage(lab))) == {0, 1, 2}
          and table[2] == "B")
    check("structures: label parsing (4 forms)",
          parse_labels("PTV=1, GTV*=2") == [("PTV", "PTV", 1), ("GTV", "GTV*", 2)]
          and parse_labels("1=PTV; 2=GTV,GTVn1") == [("PTV", "PTV", 1), ("GTV", "GTV,GTVn1", 2)]
          and parse_labels("GTV=GTV*,PTV=PTV") == [("GTV", "GTV*", None), ("PTV", "PTV", None)]
          and parse_labels("PTV=1,GTV,GTVn1=2") == [("PTV", "PTV", 1), ("GTV", "GTV,GTVn1", 2)])


def test_empty_stays_empty():
    """dicognito must not invent values for attributes that were empty (e.g. a fake clinic name)."""
    from dicognito.anonymizer import Anonymizer
    from toolsets.dicom import anonymize as A
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID = generate_uid()
    ds.SOPClassUID = CTImageStorage
    ds.PatientName, ds.PatientID, ds.Modality, ds.StudyDate = "", "", "CT", "20240115"
    ds.InstitutionName, ds.OperatorsName = "", ""          # InstitutionAddress absent on purpose
    ds.ReferringPhysicianName = PHI["physician"]
    item = Dataset()
    item.InstitutionName = ""
    ds.ReferencedStudySequence = [item]
    A.anonymize_dataset(ds, Anonymizer(seed="test"))
    check("anonymize: empty attributes stay empty, none invented (clinic, address, operator)",
          not ds.InstitutionName and "InstitutionAddress" not in ds and not ds.OperatorsName
          and not ds.ReferencedStudySequence[0].InstitutionName,
          f"{ds.get('InstitutionName')!r} {ds.get('InstitutionAddress')!r} {ds.get('OperatorsName')!r}")
    check("anonymize: filled attributes are still replaced, patient keeps a pseudonym",
          str(ds.ReferringPhysicianName) != PHI["physician"] and str(ds.PatientID) != ""
          and str(ds.PatientName).startswith("ANON^"))


GERMAN_NAMES = {   # clinical German names seen at LMU -> expected TG-263 name
    "Rückenmark": "SpinalCord", "Myelon+5mm": "SpinalCord_PRV05", "N. opticus li": "OpticNrv_L",
    "Gl. parotis rechts": "Parotid_R", "Speiseröhre": "Esophagus", "OESOPHAGUS": "Esophagus",
    "Plexus_re": "BrachialPlex_R", "Lunge li": "Lung_L", "Lungen bds": "Lungs", "Lunge-GTV": "Lungs-GTV",
    "Lungen ohne PTV": "Lungs-PTV", "HERZ": "Heart", "GROßE_GEFÄßE": "GreatVes", "ZENTRALEATEMWEGE": "Bronchus",
    "Hüftkopf rechts": "Femur_Head_R", "Harnblase": "Bladder", "Boundary": "External", "Außenkontur": "External",
    "GTV_Primarius": "GTVp", "GTV_LN_2R_4R": "GTVn_2R4R", "CTV LAW": "CTVn", "PTV_50,4": "PTV_5040",
    "PTV2_45Gy": "PTV2_4500", "H_RING": "zRING", "H_LUNGEN_BDS_GTV": "zLUNGEN_BDS_GTV", "PTV-Ring": "zPTV_Ring",
    "Luft": "zLuft", "zPTV_opt": "zPTV_opt",
}


def test_cli():
    """Every command-line tool in cli/ starts (imports resolve from the repo root) and shows its help."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    for f in sorted(os.listdir(os.path.join(root, "cli"))):
        if f.endswith(".py"):
            p = subprocess.run([sys.executable, os.path.join(root, "cli", f), "--help"], capture_output=True,
                               text=True, cwd="/tmp" if os.path.isdir("/tmp") else None, timeout=120)
            if p.returncode != 0 or "usage" not in p.stdout.lower():
                bad.append(f"{f}: {(p.stderr or p.stdout).strip().splitlines()[-1:]}")
    check("cli: all command-line tools start and show --help", not bad, "; ".join(bad))


def test_identifiers_and_names(tmp):
    """check_identifiers finds planted identifiers (without showing them); suffix markers in structure names."""
    from harness import load_toolsets
    from toolsets.dicom import structnames as N
    src = os.path.join(tmp, "phi_case", "original")
    if os.path.isdir(src):
        ts = load_toolsets([{"name": "dicom", "params": {"folder": src}}])[0]
        T = {t.name: t for t in ts.tools}
        out = T["check_identifiers"].fn()
        leaked = [v for v in ("MUSTERMANN", "ERIKA", "PID48213", "19650312") if v.lower() in out.lower()]
        check("identifiers: planted name / ID / birth date reported as present, values not shown",
              "direct patient identifiers" in out and "PatientBirthDate" in out and not leaked, out[-200:])
    want = {"Heart_P": "Heart", "Lung left_P": "Lung_L", "Total Lung_P": "Lungs", "Rectum_old": "Rectum",
            "Parotis_li_neu": "Parotid_L"}
    wrong = {n: N.standardize(n)["standard"] for n, s in want.items() if N.standardize(n)["standard"] != s}
    check("names: site/version suffixes (_P, _old, _neu) ignored", not wrong, str(wrong))


def test_thinking_harness():
    """Thinking models: empty answer -> asked once, then reasoning used; tool call written as text -> executed."""
    from harness import Agent, Toolset, tool
    from harness.core import text_tool_calls

    @tool("list_series", "x")
    def list_series():
        return "50 series"

    class Stub:
        def __init__(self, replies):
            self.replies, self.calls = list(replies), 0

        def chat(self, msgs, schemas):
            self.calls += 1
            return self.replies.pop(0)

    calls, rest = text_tool_calls("Let me check.\n<tool_call>\n<function=list_series>\n</function>\n</tool_call>",
                                  {"list_series": list_series})
    check("thinking: a tool call written as text is recognised", len(calls) == 1 and rest == "Let me check.",
          f"{calls} {rest!r}")
    m = Stub([{"content": "", "reasoning_content": "thinking..."}, {"content": "There are 50 series."}])
    ans = Agent(m, [Toolset("t", [list_series])], "sys").ask("How many?")
    check("thinking: an empty answer is asked for once", m.calls == 2 and ans == "There are 50 series.", ans)
    m = Stub([{"content": "", "reasoning_content": "a\n\nSo the answer is 50 series."},
              {"content": "", "reasoning_content": "b\n\nThe answer is 50 series."}])
    ans = Agent(m, [Toolset("t", [list_series])], "sys").ask("How many?")
    check("thinking: still empty -> the end of the reasoning, marked", ans.startswith("(no final answer written")
          and "50 series" in ans, ans)


def test_harness_text():
    from harness.core import TOOLS_NOTE
    a = "Report text.\n[tools used for this answer: list_series, resample_images, export_mha]"
    check("harness: a copied '[tools used ...]' line is removed from answers",
          TOOLS_NOTE.sub("", a).strip() == "Report text.")

    # numbers without a tool call in the turn -> the model is sent back once
    from harness import Agent, Toolset, tool

    class Stub:
        def __init__(self, replies):
            self.replies, self.calls = list(replies), 0

        def chat(self, msgs, schemas):
            self.calls += 1
            return {"content": self.replies.pop(0)}

    @tool("dummy", "x")
    def dummy():
        return "1"

    m = Stub(["For Fx6 the GTVp volume is 124.32 ml.", "I do not know without a tool."])
    ans = Agent(m, [Toolset("t", [dummy])], "sys").ask("And in Fx6?")
    check("harness: a value given without any tool call is pushed back once",
          m.calls == 2 and ans == "I do not know without a tool.", f"{m.calls} calls, answer {ans!r}")
    m = Stub(["The RTSTRUCT is P001-S01-09.", "I do not know."])
    ans = Agent(m, [Toolset("t", [dummy])], "sys").ask("Which RTSTRUCT belongs to P001-S01-11?")
    check("harness: a series id that appears nowhere is pushed back", m.calls == 2, f"{m.calls} calls")
    # "done" after only a dry run -> pushed back
    @tool("fake_export", "x", {"type": "object", "properties": {"dry_run": {"type": "boolean"}}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False)
    def fake_export(dry_run=True):
        return "plan: 34 images (dry run - nothing written)"

    class StubCall(Stub):
        def chat(self, msgs, schemas):
            self.calls += 1
            r = self.replies.pop(0)
            return r if isinstance(r, dict) else {"content": r}

    call = {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
        "name": "fake_export", "arguments": "{\"dry_run\": true}"}}]}
    m = StubCall([call, "The export has been completed.", "Dry run only: 34 images would be exported. Run it?"])
    from harness import Policy
    ans = Agent(m, [Toolset("t", [fake_export])], "sys", Policy(allow_write=True)).ask("Export it")
    check("harness: 'completed' after only a dry run is pushed back", m.calls == 3 and ans.startswith("Dry run"),
          f"{m.calls} calls, answer {ans!r}")
    m = Stub(["There are 8 structure sets."])
    ans = Agent(m, [Toolset("t", [dummy])], "sys").ask("How many?")
    check("harness: answers without measured values are not pushed back", m.calls == 1)

    # "can you ...?" asks whether: a write runs only as its dry run, nobody is asked to approve
    from harness.core import ability_question
    yes = ["Can you run the anonymization?", "Could you export the MHA files?", "能不能导出 MHA？",
           "Kannst du das anonymisieren?", "可以帮我匿名化吗"]
    no = ["Export the structures as MHA masks.", "How many series are there?", "Run the anonymization.",
          "Which plan can be used?"]
    bad = [q for q in yes if not ability_question(q)] + [q for q in no if ability_question(q)]
    check("harness: 'can you ...?' questions are recognised", not bad, f"misread: {bad}")
    asked, ran = [], []

    @tool("fake_anon", "x", {"type": "object", "properties": {"dry_run": {"type": "boolean"},
                                                            "overwrite": {"type": "boolean"}}},
          read_only=False, safe_when=lambda a: a.get("dry_run", True) is not False,
          deletes=lambda a: a.get("dry_run", True) is False and bool(a.get("overwrite")))
    def fake_anon(dry_run=True, overwrite=False):
        ran.append(dry_run)
        return "plan: 12 files" if dry_run else "anonymized 12 files"

    call = {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
        "name": "fake_anon", "arguments": "{\"dry_run\": false}"}}]}
    m = StubCall([call, "Yes: it would anonymize 12 files. Shall I run it?"])
    a = Agent(m, [Toolset("t", [fake_anon])], "sys", Policy(allow_write=True),
              confirm=lambda n, args: asked.append(n) or True)
    ans = a.ask("Can you run the anonymization?")
    check("harness: 'can you ...?' runs only the dry run and asks no approval",
          ran == [True] and not asked and ans.endswith("run it?"), f"ran={ran} asked={asked} answer={ans!r}")
    m = StubCall([call, "Anonymized 12 files."])
    a = Agent(m, [Toolset("t", [fake_anon])], "sys", Policy(allow_write=True),
              confirm=lambda n, args: asked.append(n) or True)
    a.ask("Run the anonymization.")
    check("harness: a plain request writes without asking", not asked and ran[-1] is False,
          f"ran={ran} asked={asked}")
    call_ow = {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {
        "name": "fake_anon", "arguments": "{\"dry_run\": false, \"overwrite\": true}"}}]}
    del ran[:]
    m = StubCall([call_ow, "Not replaced."])
    a = Agent(m, [Toolset("t", [fake_anon])], "sys", Policy(allow_write=True),
              confirm=lambda n, args: asked.append(n) or False)
    a.ask("Run the anonymization again and replace the old one.")
    check("harness: replacing (deleting) an earlier result asks first, and a no stops it",
          asked == ["fake_anon"] and not ran, f"ran={ran} asked={asked}")
    del asked[:]
    m = StubCall([call, "Anonymized 12 files."])
    a = Agent(m, [Toolset("t", [fake_anon])], "sys", Policy(allow_write=True, confirm_writes=True),
              confirm=lambda n, args: asked.append(n) or True)
    a.ask("Run the anonymization.")
    check("harness: confirm_writes=true asks before every write", asked == ["fake_anon"], f"asked={asked}")
    del asked[:]
    m = StubCall([call, "Anonymized 12 files."])
    a = Agent(m, [Toolset("t", [fake_anon])], "sys", Policy(allow_write=True))
    a.ask("Run the anonymization.")
    check("harness: without a confirm function a plain write still runs", ran[-1] is False, f"ran={ran}")


def test_structure_categories():
    from toolsets.dicom.mha_tools import Export
    ex = Export.__new__(Export)
    ex.structures = [dict(patient="P001", rtstruct="R1", name=n, standard=s, naming=k) for n, s, k in [
        ("BODY", "External", "synonym"), ("Herz", "Heart", "synonym"), ("Lunge li", "Lung_L", "synonym"),
        ("Myelon", "SpinalCord", "synonym"), ("PTV_60", "PTV_6000", "target"), ("GTVp", "GTVp", "target"),
        ("Ring", "zRing", "helper"), ("Tumor Bed", "Tumor Bed", "unmapped"), ("Iso", "Iso", "point")]]
    got = lambda q: sorted(s["standard"] for s in ex.find(q))
    check("structures: select='OARS' = organs only (no External, targets, helpers, unknown names)",
          got("OARS") == ["Heart", "Lung_L", "SpinalCord"], str(got("OARS")))
    check("structures: select='TARGETS' = GTV/CTV/PTV", got("TARGETS") == ["GTVp", "PTV_6000"], str(got("TARGETS")))
    check("structures: 'organs at risk' works as well", got("organs at risk") == got("OARS"))


def test_german_names():
    from toolsets.dicom import structnames as N
    wrong = {n: N.standardize(n)["standard"] for n, want in GERMAN_NAMES.items()
             if N.standardize(n)["standard"] != want}
    check(f"names: {len(GERMAN_NAMES)} German clinical names -> TG-263", not wrong, str(wrong)[:300])


def main():
    tmp = tempfile.mkdtemp(prefix="orin_tests_")
    print(f"orin-agent regression tests (synthetic data) in {tmp}")
    for name, fn in [("anonymization + export check", lambda: test_anonymization(tmp)),
                     ("layout", lambda: test_layout(tmp)), ("structures", test_structures),
                     ("German structure names", test_german_names),
                     ("anonymizer: empty attributes", test_empty_stays_empty),
                     ("harness", test_harness_text), ("structure classes", test_structure_categories),
                     ("command-line tools", test_cli),
                     ("identifiers and names", lambda: test_identifiers_and_names(tmp)),
                     ("thinking mode", test_thinking_harness)]:
        print(f"\n[{name}]")
        try:
            fn()
        except Exception as e:
            check(f"{name}: crashed", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()
    shutil.rmtree(tmp, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed" + (f", {len(failed)} FAILED" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
