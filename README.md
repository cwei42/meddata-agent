# orin-agent — a small local agent harness

Model, tools and instructions are separate pieces. The harness only runs the loop
(ask model → run tool calls → give results back → repeat) and enforces the policy.

```
orin-agent/
  harness/           generic, no domain code
    core.py          agent loop, tool dispatch, images, redaction, trace
    models.py        model backends (any OpenAI-compatible server)
    tools.py         Tool / Toolset / ToolResult + @tool decorator
    policy.py        step limits, read-only enforcement, output clipping
  toolsets/          plugins — one folder each, exposes setup(params) -> Toolset
    dicom/           list_series, get_series_details, view_thumbnail (+ scan.py)
    system/          get_system_status, get_temperatures, get_network
  prompts/           system prompts (plain text)
  configs/           model + toolsets + prompt + policy
  bin/               llm-start.sh [4b|2b], llm-stop.sh
  run.py             CLI
```

## Session space (encrypted, temporary — nothing is kept on the Orin)

Every session gets a **new** encrypted space at `/data/medical`; ending the session destroys it.
The key is 64 random bytes read once from `/dev/urandom` by cryptsetup (plain dm-crypt, AES-XTS-512): nobody
knows it, there is no passphrase, and it exists only in kernel memory. When the session ends — quitting
`orin-console`, logging out at the Orin's screen, closing the terminal, reboot or power loss — the key is gone, so
everything written in the session is unreadable random data; the container file is deleted as well (a boot
service removes leftovers of sessions that were not ended).

```bash
sudo medical-vault start          # new space (up to 400 GB, sparse: only used space takes disk)
sudo medical-vault status
sudo medical-vault end [--force]  # destroy it (--force stops programs that still use files in it)
```

Layout inside a session: **one folder per case (import), one sub-folder per processing step:**

```
/data/medical/<case>/original/     imported data, never changed
                     report/       structure report        organized/   sorted DICOM copy
                     anonymized/   anonymized DICOM        mha/         MHA images + structure masks
                     structures/   merged/expanded structures, RTSTRUCTs, label maps, DVHs
                     work/         results of the agent's own code
```
Every tool takes the case folder (or its `original/`). Folders outside a case (e.g. test data in the home folder)
keep the older `<folder>_mha`, `<folder>_derived` … next to them (`toolsets/dicom/layout.py`).

- **export results to a USB drive before ending** — nothing can be recovered afterwards
- mounted `nodev,nosuid,noexec`, owner-only; start/end are logged to syslog (`medical-vault`)
- logging out of a remote SSH connection does not touch the space; logging out at the Orin's own screen ends it
- set up with `sudo bash bin/setup-session-vault.sh` (installs the tool + boot cleanup, erases and deletes the
  old 400 GB vault with its fixed passphrase, then tests start → write → end)

## Offline console (for patient data — no network, no PC)

Patient data is processed with the Orin **offline**, operated with its own monitor and keyboard.
Text mode only (no desktop, no browser): ~30 MB instead of ~0.5–1 GB, memory stays free for the model.

Log in on the Orin's screen and run `orin-console`:

```
(on start: a new encrypted session space is created automatically)
1 Go offline / online   2 Session space status / start   3 Import from USB drive -> new case <case>/original
4 Ask the agent          5 Run a tool (report / organize / anonymize / MHA / structures)
6 Export a step of a case to USB (OrinExport/<case>/<step>)   7 Status + offline log
q Quit = destroy the session space (asks first), stop model, optionally back online
```

- `orin-offline on|off|status|test N|log` (sudo, no password): turns off WiFi, Ethernet, Bluetooth,
  Tailscale, USB-C network, Docker network, then **verifies** nothing is left (`OFFLINE ✓ (verified)`);
  every switch is logged in `/var/log/orin-offline.log`. `test N` returns online after N seconds.
- `orin-usb list|mount DEV [rw]|unmount DEV` (sudo, no password): USB drives only; **read-only** for
  import, read-write only for export; unmount powers the drive off. BitLocker drives not supported yet.
- the console warns in red while online; agent write actions need `y` like everywhere else
- installed with `bin/setup-offline-mode.sh` (already done)

## Web interface (online use only)

On Windows double-click `orin-agent-ui.bat` (starts the UI on the Orin, opens an SSH tunnel, opens
http://localhost:8090). Or manually:

```bash
~/orin-agent/bin/ui-start.sh           # on the Orin (127.0.0.1:8090 only)
ssh -N -L 8090:127.0.0.1:8090 orin     # on the PC, then open http://localhost:8090
~/orin-agent/bin/ui-stop.sh
```

- start/stop the model (4B / 2B), pick a config, fill its fields (folder browser limited to ~, /media, /mnt)
- chat; every tool call is shown live and can be expanded
- write actions appear as an **Approve / Decline** card (no answer in 5 min = declined)
- reports (summary, anonymization, export) open from the sidebar, redacted
- thumbnails the model looks at are **not** sent to the browser unless a config sets `"ui": {"show_images": true}`
- server listens on 127.0.0.1 only; access goes through the key-protected SSH tunnel (USB or Tailscale)

## Run (command line)

```bash
cd ~/orin-agent
bin/llm-start.sh 4b
~/dicom-env/bin/python run.py configs/dicom.json --set folder=/path/to/dicom         # interactive
~/dicom-env/bin/python run.py configs/dicom.json --set folder=/path -q "有没有X光片？"
~/dicom-env/bin/python run.py configs/system.json -q "Wie warm ist die GPU?"
~/dicom-env/bin/python run.py configs/dicom.json --set folder=/path --toolset system  # combine toolsets
bin/llm-stop.sh
```

Options: `--model-url URL` (other model server), `--trace file.jsonl` (log questions, tool calls,
answers — contains data, keep it with the data), `--quiet` (hide `[tool]` lines).

## Organize DICOM folders

Copies a messy folder into a clean structure (originals are never changed):

```
<case>/organized/
  P001/S02_2001_XR_C_Spine/001_CR_Cervical_LAT/IM0001.dcm   (images sorted by slice position)
  manifest.csv     source -> destination for every file, duplicates listed
  summary.txt      what was done, verification result
```

Without LLM (deterministic, shows the plan and asks):
```bash
~/dicom-env/bin/python dicom_organize.py /path/to/dicom [-o /path/to/output]
```

With the agent (the model can plan; writing needs your approval):
```bash
~/dicom-env/bin/python run.py configs/dicom-write.json --set folder=/path [--set output=/path/out]
```
The write config offers three write tools: `organize_dicom`, `anonymize_dicom`, `export_mha`.

## Anonymize (DICOM → anonymized DICOM)

```bash
~/dicom-env/bin/python dicom_anonymize.py /path/to/dicom [-o OUT] [--seed SECRET] [--keep-private] [--write-mapping]
```
Output `<case>/anonymized/` (Patient/Study/Series, no years in folder names) + `anonymization_report.txt`.

- core: [dicognito](https://github.com/blairconrad/dicognito) — names, IDs, addresses, institutions replaced;
  **all dates shifted by one offset** (intervals kept); **all UIDs remapped consistently** (series, RTSTRUCT →
  CT references stay valid)
- extra rules (gaps found in testing): free text (descriptions, comments, protocol) masked for the patient's
  name/ID/birth date; device serial, physician/operator names etc. emptied **also inside sequences**
  (e.g. RT plan beams); private tags removed; PatientName = `ANON^<new id>`; raw files without file meta fixed
- **verification:** every output file is re-read and checked against its own patient's original identifiers
- seed: random and not stored by default → irreversible. With `--seed` the same patient gets the same pseudonym in
  later runs (keep the seed secret). `--write-mapping` writes original→new IDs (protect that file)
- **pixel data is not changed** — the report lists series that may have burned-in text (US, screenshots, SC)

## Export to MHA (plastimatch) — images and RTSTRUCT masks

```bash
~/dicom-env/bin/python dicom_to_mha.py /path/to/dicom --list                    # see series ids
~/dicom-env/bin/python dicom_to_mha.py /path/to/dicom                           # reference found automatically
~/dicom-env/bin/python dicom_to_mha.py /path/to/dicom --ref P001-S01-02          # this CT for all RTSTRUCTs
~/dicom-env/bin/python dicom_to_mha.py /path/to/dicom --ref P001-S01-05=/data/ct.mha   # per RTSTRUCT, image file
```
Output `<case>/mha/`: `<series>.mha` + `<series>.json` (safe technical parameters only),
`<rtstruct>/<structure>.mha` (one binary mask per ROI on the reference grid) + `structures.json`, and
`export_report.txt` (exported / skipped with reason / failed).

- reference for RTSTRUCT: `--ref` (series id, DICOM folder or image file) → else the series named in the
  RTSTRUCT (RTReferencedSeriesSequence) → else same FrameOfReferenceUID (CT preferred)
- MHA headers are rewritten to geometry keys only; JSON has no dates, UIDs or names
- skipped: localizers, US/screenshots/reports, series with several images per slice position (4D, multi-echo, DWI)
- plastimatch is built from source in `~/opt/plastimatch` (ITK 5.3 in `~/opt/itk`), without CUDA
  (`~/build_plastimatch.sh`; fixes needed on Ubuntu 24.04: `#include <cstdint>` in
  `itkMathematicalMorphologyEnums.h`, `-DPLM_CONFIG_ENABLE_CUDA=OFF`, package `libnsl-dev`)

Tested 2026-11-24 on synthetic data (`~/dicom-tools/make_rt_test.py` → CT + decoy CT with the same
FrameOfReference + RTSTRUCT with GTV sphere / PTV box; `check_rt_test.py CT.mha MASKDIR` checks grid and Dice):

| case | reference chosen | result |
|---|---|---|
| automatic | planning CT via RTReferencedSeriesSequence (not the decoy) | GTV Dice 0.994, PTV 0.959, same grid |
| `--ref P001-S01-02` / `--ref ID=file.mha` | coarse CT (2.5×2.5×5 mm) | in-plane identical in every slice; GTV 0.976 |
| agent `export_mha` | coarse CT as asked; dry run first, writes only after "yes" | same as above |

On a coarser reference grid a structure can gain one slice at each end (slice centre within half a slice of
the last contour) — normal rasterization, not misalignment. MHA headers contain geometry keys only.

Mask files are named by **AAPM TG-263** (next section); `--original-names` keeps the original names,
`--names-map FILE` uses another mapping file. `structures.json` keeps original name → standard name.

## Structure names (AAPM TG-263)

```bash
~/dicom-env/bin/python dicom_structures.py /path/to/dicom [--unmapped todo.csv] [--all]   # report only
```
Rules (deterministic, no model): German/English synonyms and abbreviations (`Rückenmark`, `Myelon` →
`SpinalCord`, `Parotis li` → `Parotid_L`, `Lungen bds` → `Lungs`, `BODY` → `External`), laterality,
PRV margins (`Myelon PRV5` → `SpinalCord_PRV05`), targets rebuilt as Type[classifier]_[dose cGy]
(`PTV 60Gy` → `PTV_6000`, `CTV_n 54` → `CTVn_5400`), helpers get the `z` prefix (`Ring 2cm` → `zRing_2cm`).

| status | meaning |
|---|---|
| mapping | from `configs/structure_names.csv` (clinic list, always wins) |
| standard / synonym | sure |
| target / helper | renamed by rule — **review** |
| point | isocenter / reference point, name kept |
| unmapped | not recognised, name kept (e.g. `parotis` without side) |

Unknown names: the export writes `structure_names_unmapped.csv`; fill in the `standard` column and add the rows
to `configs/structure_names.csv` — next time they are mapped. Duplicates of one standard name get `_2`.
DICOM files are never changed. Agent tool: `list_structures` (read-only).

Safety: output path comes from the config, never from the model · output must be empty and outside the
input · free space checked first · never overwrites a file · every copy size-verified · duplicate
SOPInstanceUIDs copied once · folder names use pseudonyms and masked descriptions.
**Organizing is not anonymizing:** the copied files still contain all original header data.

## Structure tools (merge, margins, Dice, dose, labels) — no model needed

Works on the MHA export (`dicom_to_mha.py` first). Results go to `<case>/structures/<patient>/`, never overwritten,
each with a JSON saying what was done (inputs, operation, margin, reference CT, volume).

```bash
T="$HOME/dicom-env/bin/python structure_tools.py /data/medical/<case>"
$T list                                            # structures + images per patient
$T stats "GTV*"                                    # volume ml, centroid, extent, parts, touches border
$T compare "GTV" "GTV_obs2"                        # Dice, Jaccard, Hausdorff, HD95, mean surface distance
$T values "PTV" [--image P001-S01-04 --v 20,30]    # HU stats, or for RTDOSE: Dmean, D98/95/50/2 %, Vx Gy
$T dvh "PTV,Heart" --dose P001-S01-04              # cumulative DVH as CSV
$T merge "GTV*" --name GTV_all [--op union|intersection|minus] [--margin 5] [--rtstruct]
$T margin "GTV_all" --mm 5 [--rtstruct]            # -3 shrinks; 5,5,10 = x,y,z (mm)
$T labelmap "CTV=CTV" "GTV=GTV*" --name labels     # one label file (later label wins on overlap) + label table
$T crop "PTV" --margin 20                          # reference image + all masks around PTV (DL patches)
$T resample --spacing 1,1,1                        # all images (linear) and masks (nearest)
```
- select by TG-263 name, original name or glob, several with `,`; glob patterns skip helper structures (`z…`);
  results of merge/margin can be used again by name (e.g. `GTV_all` → `margin GTV_all`)
- structures on different grids are resampled onto the first (reported); dose/images linearly onto the mask
- margins: Euclidean distance on a half-voxel grid around the structure (so 3 mm slices do not lose 1.5 mm)
- `--rtstruct`: new RTSTRUCT on the reference CT (plastimatch) — same FrameOfReference, patient and study as the
  CT so a TPS can import it; it therefore **contains the patient identifiers of that CT** (stays in the vault)
- agent tools (in the dicom toolset, like `list_series`): read-only `structure_stats`, `compare_structures`,
  `structure_values`; with approval `merge_structures`, `expand_structure`, `make_labelmap`, `crop_to_structure`,
  `resample_images`, `export_dvh`. Console: 5 → "Structure tools"

Tested on synthetic data (`~/gtv-test`, sphere GTV r 15 mm, CTV r 20 mm, two node GTVs, PTV box):

| check | result |
|---|---|
| union / intersection / minus | exact voxel counts (2473; CTV∩GTV = GTV; CTV−GTV = 2919) |
| GTV + 5 mm vs CTV (r 20) | Dice 0.97, HD95 1.5 mm (= half slice), mean surface distance 0.2 mm |
| CTV − 5 mm vs GTV (r 15) | Dice 0.92, HD95 1.5 mm, mean surface distance 0.4 mm |
| margin 0,0,6 mm | z extent 27 → 39 mm, x/y unchanged |
| HU in GTV / dose in GTV | 1000 HU / 60 Gy exactly (phantom values); coarse CT resampled and reported |
| merged RTSTRUCT → mask again (plastimatch) | Dice 1.0, identical volume |

## Agent writes its own code (run_python, skills)

For tasks no tool covers, the agent writes a Python script; **you read it and approve it** before it runs.

```bash
~/dicom-env/bin/python run.py configs/python.json --set folder=/data/medical/<case>
```
In `orin-console` → 4 (agent) answer **y** to "Allow the agent to write Python code?". The script is shown with
line numbers; `Run this code? [y/N]`. `--yes` never approves code.

Sandbox (bubblewrap): dataset **read-only** at `/data/input`, writes only to `<case>/work` (`/data/work`),
**no network**, no home folder / SSH keys / other datasets, numpy + pydicom + SimpleITK + pillow only,
memory 3 GB, 300 s per run. If the sandbox cannot start, nothing runs. One-time setup (AppArmor profile that lets
`bwrap` create namespaces on Ubuntu 24.04): `sudo bash ~/orin-agent/bin/setup-sandbox.sh`.

How the model writes code (ideas taken from smolagents / Qwen-Agent): it answers with `Thought:` and one
```` ```python ```` block in plain text — no script escaped inside JSON (that broke on the 4B model) — and gets an
`Observation` back; cut-off code blocks are never run. Inside the code `SERIES` gives every series with the same
ids as `list_series` (`{id: {modality, category, description, files}}`), so scripts and tools agree.

Structures: after `export_mha`, the code gets `STRUCTURES` (patient, original name, TG-263 name, mask path) and
small tested helpers — `combine_masks(…, op='union'|'intersection'|'minus')`, `mask_volume_ml`, `read_image`,
`write_image` (into WORK). The MHA export is mounted read-only. **Geometry guard:** `write_image` refuses, and a
check after every run flags, any image whose size/spacing/origin matches no exported CT/mask (a numpy array saved
without the reference geometry has the right voxel count but does not line up with the patient — this happened in
testing). Such a run does not count as "worked" and cannot be saved as a skill.
Tested on synthetic data (`~/gtv-test`: GTV, GTVn 1, GTV_LN2 + PTV, CTV, zGTV_ring): "merge all GTVs into one
label" → export_mha on the planning CT, union of the 3 GTVs (ring/PTV/CTV left out), 16.7 ml, voxel-identical to
the expected union, same grid as the CT.

Skills: every script that worked gets a number (#1, #2 …). `save_skill` keeps one (default: the last one that
wrote files) in `~/orin-agent/skills/<name>.py` — **the approval shows exactly the code that will be saved**.
Later the agent finds it with `list_skills` and runs it with `run_skill` (values that change come from `ARGS`) —
no new code, no new approval. Skills are plain files: read, edit or delete them yourself.
Example from testing: `skills/ct_summary.py` (slice thickness + number of slices of every CT series → CSV).

Harness guards for the small model: a call repeated with identical arguments is not run again; on the last
step no tools are offered so it must answer; old outputs and old script versions are shortened to stay inside
the 8k context; a cut-off tool call is retried with "use a shorter script".

The 4B model writes simple scripts (counting, filtering, tables, conversions) reasonably, but makes mistakes:
read the code, check results. Anonymization, structure naming and organizing stay with the tested tools above.

Model server memory: `bin/llm-start.sh` runs llama-server with `-np 1 --cache-ram 512`. The default prompt cache
(8 GB) is the whole RAM of the Orin; long agent runs got llama-server killed by the out-of-memory killer.

## Swap the model

Models are just servers. Start another one and point the config (or `--model-url`) at it:

```bash
bin/llm-start.sh 2b                     # faster, less accurate
```
Any OpenAI-compatible endpoint works (llama-server, vLLM, Ollama). New backends go in `harness/models.py`.

## Add a toolset

Create `toolsets/<name>/__init__.py`:

```python
from harness import Toolset, ToolResult, tool

def setup(params):                       # params come from the config
    @tool("count_files", "Count files in the configured folder.",
          {"type": "object", "properties": {"suffix": {"type": "string"}}})
    def count_files(suffix=""):
        import os
        n = sum(f.endswith(suffix) for _, _, fs in os.walk(params["folder"]) for f in fs)
        return f"{n} files"              # or ToolResult(text, images=[data_url])

    return Toolset(name="files", tools=[count_files],
                   redact=None,           # optional: function applied to tool output + final answer
                   describe="file counter")
```

Then reference it in a config: `"toolsets": [{"name": "files", "params": {"folder": "${folder}"}}]`.

## Policy (enforced by the harness, not the model)

- `max_steps` model turns and `max_tool_calls` per question
- write tools are hidden unless the config sets `"allow_write": true` (only `configs/dicom-organize.json` does)
- every write action needs your approval at the terminal (`y/N`); a tool can mark harmless calls as
  not needing approval (`safe_when`, e.g. a dry run). Without a terminal, writes are refused unless `--yes`
- tool output is clipped to `max_tool_output` characters
- each toolset's `redact` runs on every tool result and on the final answer
  (dicom: every patient-name part found in the folder becomes `[NAME]`, even if the model read it from an image)

## Privacy notes

- Everything runs on the Orin; the model server listens on 127.0.0.1 only.
- llama-server's own built-in tools (file/shell access) are not enabled.
- With real patient data: run it yourself on the Orin, keep traces and answers with the data.
