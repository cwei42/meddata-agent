#!/usr/bin/env python3
"""
orin-agent offline console - text-mode workflow on the Orin's own screen and keyboard.
No browser, no desktop: ~30 MB instead of ~0.5-1 GB, leaving memory for the model.

  ~/dicom-env/bin/python ~/orin-agent/console.py      (or just: orin-console)

Workflow: start -> a NEW encrypted session space is created (random key, nothing kept) -> go offline ->
import from USB drive into a case -> ask the agent / run tools -> export results to USB drive ->
quit = the session space is destroyed (also on logout, reboot, power loss) -> back online (optional).
Layout: /data/medical/<case>/original (import), /data/medical/<case>/<step> (report, mha, structures, ...).
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from toolsets.dicom import layout as L   # noqa: E402

from rich.console import Console          # noqa: E402
from rich.markdown import Markdown         # noqa: E402
from rich.panel import Panel               # noqa: E402
from rich.prompt import Confirm, Prompt    # noqa: E402
from rich.table import Table               # noqa: E402

PY = sys.executable
VAULT = "/data/medical"
IDENTIFIABLE_STEPS = ("original", "organized", "structures")   # contain identifiers by design
IDLE_MIN = int(os.environ.get("ORIN_IDLE_MIN", "30"))             # idle at a prompt -> session destroyed
c = Console()


# ------------------------------------------------------------------ helpers
def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def offline_state():
    r = sh(["sudo", "-n", "/usr/local/sbin/orin-offline", "status"])
    return r.returncode == 0, r.stdout.strip()


VAULT_CMD = ["sudo", "-n", "/usr/local/sbin/medical-vault"]


def vault_unlocked():
    """True while a session space is active (kept name: used everywhere)."""
    return os.path.ismount(VAULT)


def session_start():
    if vault_unlocked():
        return True
    with c.status("creating a new encrypted session space …"):
        r = sh(VAULT_CMD + ["start"])
    c.print(r.stdout.strip() or r.stderr.strip())
    if vault_unlocked():
        L.use_session_tmp()                                 # all temp files inside the encrypted space
    return vault_unlocked()


def run_script(name, *args):
    """bin/<name> via bash, so a lost executable bit (e.g. a checkout from Windows) cannot break it."""
    return subprocess.run(["bash", os.path.join(HERE, "bin", name), *args])


def session_end(force=False):
    # Stopping the model must never prevent the destruction below: an exception here once left the session
    # space open after the console was closed.
    try:
        if model_running():
            run_script("llm-stop.sh")                                # the model must not hold files open
    except Exception as e:
        c.print(f"[yellow]could not stop the model ({e}) - ending the session anyway[/]")
    os.chdir(HERE)
    r = sh(VAULT_CMD + ["end"] + (["--force"] if force else []))
    c.print(r.stdout.strip() or r.stderr.strip())
    return not vault_unlocked()


def model_running():
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2)
        return True
    except Exception:
        return False


def mem_free_mb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable"):
            return int(line.split()[1]) // 1024
    return 0


def header():
    off, _ = offline_state()
    v = vault_unlocked()
    vfree = f" · {shutil.disk_usage(VAULT).free // 2**30} GB free" if v else ""
    t = Table.grid(padding=(0, 3))
    state = "[bold green]OFFLINE ✓[/]" if off else \
        "[bold magenta]ONLINE · TEST MODE[/]" if TEST["on"] else "[bold red]ONLINE ⚠[/]"
    t.add_row(
        state,
        f"[green]Session space active{vfree}[/]" if v else "[yellow]No session space[/]",
        "[green]Model running[/]" if model_running() else "[dim]Model stopped[/]",
        f"[dim]RAM free {mem_free_mb()} MB[/]")
    c.print(Panel(t, title="[bold]Orin offline console[/]",
                  border_style="green" if off else "magenta" if TEST["on"] else "red"))


def pick(title, items, show=lambda x: str(x)):
    if not items:
        c.print(f"[yellow]{title}: nothing found[/]")
        return None
    c.print(f"[bold]{title}[/]")
    for i, it in enumerate(items, 1):
        c.print(f"  [cyan]{i}[/]  {show(it)}")
    s = Prompt.ask("number (empty = cancel)", default="")
    if s.isdigit() and 1 <= int(s) <= len(items):
        return items[int(s) - 1]
    return None


def pick_dataset():
    if not vault_unlocked():
        c.print("[yellow]No session space - start one (menu 2).[/]")
        return None
    d = pick("Case in /data/medical", list_cases(),
             lambda n: f"{n}   [dim]{' '.join(s for s in L.STEPS if os.path.isdir(os.path.join(VAULT, n, s)))}[/]")
    return os.path.join(VAULT, d) if d else None


def list_cases():
    try:
        return sorted(d for d in os.listdir(VAULT) if os.path.isdir(os.path.join(VAULT, d, L.ORIGINAL)))
    except OSError:
        return []


def copy_tree(src, dst, manifest=False):
    """Copy with verification: every copied file is read back and its SHA-256 compared with the source.
    manifest=True also writes MANIFEST.sha256 (sha256sum format) into dst."""
    import hashlib

    def sha(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(4 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    files = sorted(os.path.join(r, f) for r, _, fs in os.walk(src) for f in fs)
    total = sum(os.path.getsize(f) for f in files)
    if shutil.disk_usage(os.path.dirname(dst.rstrip("/")) or "/").free < total * 1.05:
        c.print("[red]not enough free space[/]")
        return False
    done, lines = 0, []
    with c.status("") as st:
        for i, f in enumerate(files, 1):
            rel = os.path.relpath(f, src)
            out = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            h_src = sha(f)
            shutil.copy2(f, out)
            if sha(out) != h_src:
                c.print(f"[red]checksum mismatch after copying: {out}[/]")
                return False
            lines.append(f"{h_src}  {rel}")
            done += os.path.getsize(f)
            st.update(f"copying + verifying {i}/{len(files)} files  {done / 2**20:.0f}/{total / 2**20:.0f} MB")
    if manifest:
        with open(os.path.join(dst, "MANIFEST.sha256"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
    c.print(f"[green]copied {len(files)} files ({total / 2**20:.0f} MB), every file checksum-verified[/]")
    return True


def usb_drives():
    r = sh(["sudo", "-n", "/usr/local/sbin/orin-usb", "list"])
    return [dict(zip(["dev", "size", "fs", "label", "mnt"], l.split("|"))) for l in r.stdout.splitlines() if l]


def usb_mount(rw=False):
    drives = usb_drives()
    d = pick("USB drives", drives, lambda x: f"{x['dev']}  {x['size']}  {x['fs']}  {x['label'] or ''}")
    if not d:
        return None, None
    r = sh(["sudo", "-n", "/usr/local/sbin/orin-usb", "mount", d["dev"]] + (["rw"] if rw else []))
    if r.returncode != 0:
        c.print(f"[red]{r.stdout.strip() or r.stderr.strip()}[/]")
        return None, None
    mnt = r.stdout.strip().splitlines()[-1]
    c.print(f"mounted {'read-write' if rw else '[bold]read-only[/]'} at {mnt}")
    return d["dev"], mnt


def usb_unmount(dev):
    r = sh(["sudo", "-n", "/usr/local/sbin/orin-usb", "unmount", dev])
    c.print(r.stdout.strip() or r.stderr.strip())


def browse_dir(root):
    cur = root
    while True:
        subs = sorted(d for d in os.listdir(cur) if os.path.isdir(os.path.join(cur, d)) and not d.startswith("."))
        c.print(f"\n[bold]{cur}[/]")
        c.print("  [cyan]0[/]  ✔ use this folder" + ("" if cur == root else "      [cyan]..[/]  up"))
        for i, s in enumerate(subs[:60], 1):
            c.print(f"  [cyan]{i}[/]  📁 {s}")
        a = Prompt.ask("choice (empty = cancel)", default="")
        if a == "":
            return None
        if a == "0":
            return cur
        if a == ".." and cur != root:
            cur = os.path.dirname(cur)
        elif a.isdigit() and 1 <= int(a) <= len(subs):
            cur = os.path.join(cur, subs[int(a) - 1])


# ------------------------------------------------------------------ actions
def act_offline(on):
    r = subprocess.run(["sudo", "-n", "/usr/local/sbin/orin-offline", "on" if on else "off"])
    if on and r.returncode != 0:
        c.print("[red]Not fully offline - do not process patient data.[/]")


def act_vault():
    if vault_unlocked():
        c.print("A session space is active. It is destroyed when you quit (q).")
    else:
        session_start()


# Test mode: work while online, for anonymized or synthetic data only. Off at every start unless
# `orin-console --test` or ORIN_ALLOW_ONLINE=1; switched in the menu (8) after typing TEST; every switch is logged.
TEST = {"on": "--test" in sys.argv or os.environ.get("ORIN_ALLOW_ONLINE") == "1"}


def log_event(msg):
    """One line to the system journal (journalctl -t orin-console); no patient data in it."""
    subprocess.run(["logger", "-t", "orin-console", msg], check=False)


def require_offline(what):
    """Patient data is only handled offline (verified) - except in test mode (anonymized/synthetic data)."""
    off, _ = offline_state()
    if off:
        return True
    if TEST["on"]:
        c.print(f"[magenta]Test mode: {what} while online (anonymized or synthetic data only).[/]")
        return True
    c.print(f"[red]Not offline - {what} is only possible offline. Go offline first (menu 1), "
            f"or switch on test mode (menu 8) for anonymized/synthetic data.[/]")
    return False


def act_test_mode():
    if TEST["on"]:
        TEST["on"] = False
        log_event("test mode OFF")
        c.print("[green]Test mode off - patient data work needs the Orin offline again.[/]")
        return
    c.print(Panel("Test mode lets you import, analyse and export [bold]while the Orin is online[/].\n"
                  "Use it only for [bold]anonymized or synthetic[/] data - never for identifiable patient data.\n"
                  "It stays on until you switch it off (8) or quit; the switch is logged.",
                  title="Test mode", border_style="magenta"))
    if Prompt.ask("Type TEST to confirm the data is anonymized or synthetic", default="") == "TEST":
        TEST["on"] = True
        log_event("test mode ON (anonymized/synthetic data only)")
        c.print("[magenta]Test mode on.[/]")
    else:
        c.print("[dim]not switched on[/]")


def next_case_name():
    day = time.strftime("%Y%m%d")
    n = 1
    while os.path.exists(os.path.join(VAULT, f"case-{day}-{n:02d}")):
        n += 1
    return f"case-{day}-{n:02d}"


def act_import():
    if not vault_unlocked():
        c.print("[yellow]No session space - start one (menu 2).[/]")
        return
    if not require_offline("importing data"):
        return
    dev, mnt = usb_mount(rw=False)
    if not dev:
        return
    try:
        src = browse_dir(mnt)
        if not src:
            return
        c.print("[dim]The case name appears in folder names - use a pseudonym, never a patient name.[/]")
        name = Prompt.ask("case name", default=next_case_name())
        name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name).strip("._") or next_case_name()
        if name == os.path.basename(src.rstrip("/")) and not Confirm.ask(
                "This is the folder name from the drive - it may be a patient name. Use it anyway?", default=False):
            name = next_case_name()
        case = os.path.join(VAULT, name)
        if os.path.exists(case):
            c.print(f"[red]case {name} already exists - choose another name[/]")
            return
        if not copy_tree(src, os.path.join(case, L.ORIGINAL)):
            return
        # the case name must not contain a name, ID or birth date of the imported patients
        from toolsets.dicom import export_check as X
        with c.status("checking the case name against the imported data …"):
            ids = X.identifiers(os.path.join(case, L.ORIGINAL))
        if X.scan_name(name, ids):
            new = next_case_name()
            os.rename(case, os.path.join(VAULT, new))
            c.print(f"[yellow]The name contained a patient identifier - case renamed to [bold]{new}[/].[/]")
            name, case = new, os.path.join(VAULT, new)
        c.print(f"case [bold]{name}[/]: data in {case}/{L.ORIGINAL}; each processing step gets its own "
                f"folder in {case}/")
    finally:
        usb_unmount(dev)


def act_export():
    if not vault_unlocked():
        c.print("[yellow]No session space - start one (menu 2).[/]")
        return
    if not require_offline("exporting"):
        return
    case = pick_dataset()
    if not case:
        return
    steps = [s for s in L.STEPS if os.path.isdir(os.path.join(case, s))]
    step = pick(f"Result of {os.path.basename(case)} to export", steps)
    if not step:
        return
    src = os.path.join(case, step)
    # last check: search everything to export for the case's original identifiers
    from toolsets.dicom import export_check as X
    with c.status("checking the files for patient identifiers …"):
        ids = X.identifiers(os.path.join(case, L.ORIGINAL))
        res = X.scan(src, ids)
    c.print(Panel(X.summary(res), title=f"identifier check: {step}",
                  border_style="red" if res["hits"] else "yellow" if res.get("note") else "green"))
    if res.get("note") and not res["hits"]:
        # nothing to compare against (e.g. data that arrived already anonymized): the check proves nothing
        c.print("[yellow]The identifier check could NOT run: the original data has no names, IDs or birth dates "
                "to search for (probably anonymized before import). Make sure the files are anonymized.[/]")
        if not Confirm.ask("Export anyway?", default=False):
            c.print("not exported")
            return
    identifiable = step in IDENTIFIABLE_STEPS or os.path.isdir(src) and any(
        d.startswith("RTSTRUCT") for _, ds, _ in os.walk(src) for d in ds)
    if res["hits"] and not identifiable:
        c.print(f"[red]NOT exported: '{step}' should contain no patient identifiers but does (see above). "
                "Fix it first (e.g. anonymize again).[/]")
        return
    if identifiable:
        c.print(f"[yellow]'{step}' contains patient identifiers by design (not anonymized"
                + (", e.g. RTSTRUCT for the TPS" if step == "structures" else "") + ").[/]")
        if Prompt.ask("Type IDENTIFIABLE to export it anyway (only to an encrypted / permitted drive)",
                      default="") != "IDENTIFIABLE":
            c.print("not exported")
            return
    if res["unchecked"]:
        c.print(f"[yellow]{len(res['unchecked'])} file(s) could not be checked automatically "
                "(e.g. images) - check them visually.[/]")
    sel = os.path.join(os.path.basename(case), step)
    dev, mnt = usb_mount(rw=True)
    if not dev:
        return
    try:
        dst = os.path.join(mnt, "OrinExport", sel)
        if os.path.exists(dst):
            c.print(f"[red]{dst} already exists on the drive[/]")
            return
        if Confirm.ask(f"Copy {sel} to {dst}?", default=True) and copy_tree(src, dst, manifest=True):
            with open(os.path.join(dst, "EXPORT_INFO.txt"), "w") as fh:
                fh.write(f"case: {os.path.basename(case)}\nstep: {step}\n"
                         f"exported: {time.strftime('%Y-%m-%d %H:%M')}\ntool: orin-agent {L.version()}\n"
                         f"files: {res['files']}  identifier check: {res['checked']} checked, "
                         f"{len(res['hits'])} with identifiers, {len(res['unchecked'])} not checkable\n"
                         f"identifiable by design: {'yes' if identifiable else 'no'}\n"
                         "checksums: MANIFEST.sha256 (verify with: sha256sum -c MANIFEST.sha256)\n")
            c.print(f"[green]exported to {dst} (+ MANIFEST.sha256, EXPORT_INFO.txt)[/]")
    finally:
        usb_unmount(dev)


def ensure_model():
    if model_running():
        return True
    size = Prompt.ask("Start model", choices=["4b", "2b"], default="4b")
    run_script("llm-start.sh", size)
    return model_running()


def act_agent():
    if not require_offline("working with patient data"):
        return
    ds = pick_dataset()
    if not ds or not ensure_model():
        return
    from harness import Agent, Policy, load_toolsets, make_model
    cfg = json.loads(open(os.path.join(HERE, "configs", "dicom-write.json")).read()
                     .replace("${folder}", ds).replace("${output:}", "").replace("${anon_output:}", "")
                     .replace("${mha_output:}", ""))
    if Confirm.ask("Allow the agent to write Python code? (sandboxed; you read and approve every script)",
                   default=False):
        py = json.loads(open(os.path.join(HERE, "configs", "python.json")).read())
        cfg["toolsets"].append({"name": "python", "params": {"folder": ds}})
        cfg["system_prompt"], cfg["policy"] = py["system_prompt"], py["policy"]
        cfg["model"]["max_tokens"] = py["model"]["max_tokens"]
    status = {"s": None}

    def on_event(ev):
        if ev["type"] == "tool_call":
            a = ev["args"]
            args = f"purpose={a.get('purpose', '')!r}" if "code" in a else \
                ", ".join(f"{k}={v!r}" for k, v in a.items())
            c.print(f"  [dim]🔧 {ev['name']}({args})[/]")
        elif ev["type"] == "declined":
            c.print(f"  [yellow]blocked {ev['name']} (not approved)[/]")

    def confirm(name, args):
        if status["s"]:
            status["s"].stop()
        try:
            if "code" in args:
                from rich.syntax import Syntax
                c.print(Panel(Syntax(args["code"], "python", line_numbers=True, word_wrap=True),
                              title=f"🐍 {name}: {args.get('purpose', '')}"[:120], border_style="yellow",
                              subtitle="sandbox: dataset read-only, writes only to <dataset>_work, no network"))
                return Confirm.ask("Save this code as a skill?" if name == "save_skill" else "Run this code?",
                                   default=False)
            c.print(Panel(f"[bold]{name}[/]({args})\nOriginals are not changed; output goes next to the dataset.",
                          title="✏️ Write action - approval needed", border_style="yellow"))
            return Confirm.ask("Approve?", default=False)
        finally:
            if status["s"]:
                status["s"].start()

    with c.status("scanning dataset…"):
        ts = load_toolsets(cfg["toolsets"])
    agent = Agent(make_model(cfg["model"]), ts, open(os.path.join(HERE, cfg["system_prompt"])).read(),
                  Policy(**cfg["policy"]), log=lambda s: None, confirm=confirm, on_event=on_event)
    c.print(f"[green]{ts[0].describe}[/]\nAsk questions; empty line returns to the menu.")
    while True:
        q = Prompt.ask("\n[bold cyan]?[/]", default="")
        if not q.strip():
            return
        with c.status("model is thinking…") as st:
            status["s"] = st
            ans = agent.ask(q)
        status["s"] = None
        c.print(Panel(Markdown(ans), border_style="blue"))


def act_tool():
    if not require_offline("working with patient data"):
        return
    ds = pick_dataset()
    if not ds:
        return
    t = pick("Tool (no model needed)", [
        ("Structure report (HTML/CSV)", ["toolsets/dicom/scan.py", L.input_dir(ds), "-o", L.out(ds, "report")]),
        ("Organize (copy into Patient/Study/Series)", ["dicom_organize.py", ds]),
        ("Anonymize (DICOM copy)", ["dicom_anonymize.py", ds]),
        ("Export to MHA (+ RTSTRUCT masks)", ["dicom_to_mha.py", ds]),
        ("Structure tools (merge, margin, stats, Dice, dose, labels, crop)", None),
    ], lambda x: x[0])
    if t and t[1] is None:
        act_structures(ds)
    elif t and t[1][0] == "dicom_to_mha.py":
        m = pick("Put the structure masks on", [
            ("auto", "the image each structure set refers to (recommended)"),
            ("CT", "the CT of the same plan"), ("MR", "the MR of the same plan")], lambda x: f"{x[0]:5} {x[1]}")
        if m:
            subprocess.run([PY, os.path.join(HERE, "dicom_to_mha.py"), ds, "--ref-modality", m[0]], cwd=HERE)
    elif t:
        subprocess.run([PY, os.path.join(HERE, t[1][0])] + t[1][1:], cwd=HERE)


def act_structures(ds):
    """structure_tools.py with a few questions instead of a command line."""
    if not os.path.isdir(L.out(ds, "mha")):
        c.print("[yellow]No MHA export yet - run 'Export to MHA' first.[/]")
        return
    run = lambda *a: subprocess.run([PY, os.path.join(HERE, "structure_tools.py"), ds, *a], cwd=HERE)
    run("list")
    t = pick("Structure tool", [
        ("stats    - volume, centroid, extent", "stats"), ("compare  - Dice, HD95 between two", "compare"),
        ("values   - HU or dose (D95, V20) inside", "values"), ("merge    - union / intersection / minus", "merge"),
        ("margin   - expand or shrink (mm)", "margin"), ("labelmap - several structures in one label file", "labelmap"),
        ("crop     - image + masks around a structure", "crop"), ("resample - new voxel spacing", "resample"),
    ], lambda x: x[0])
    if not t:
        return
    cmd = t[1]
    c.print("[dim]select by name or pattern, e.g. GTV*  or  Parotid_L,Parotid_R[/]")
    if cmd == "compare":
        run(cmd, Prompt.ask("first"), Prompt.ask("second"))
    elif cmd == "values":
        img = Prompt.ask("image/dose series id (empty = reference CT)", default="")
        v = Prompt.ask("Vx levels in Gy for dose (e.g. 20,30; empty = none)", default="")
        run(cmd, Prompt.ask("structures"), *(["--image", img] if img else []), *(["--v", v] if v else []))
    elif cmd == "merge":
        sel = Prompt.ask("structures to merge", default="GTV*")
        op = Prompt.ask("operation", choices=["union", "intersection", "minus"], default="union")
        name = Prompt.ask("name of the new structure", default="merged")
        m = Prompt.ask("extra margin mm (0 = none)", default="0")
        rt = Confirm.ask("also write an RTSTRUCT (for the TPS)?", default=False)
        run(cmd, sel, "--op", op, "--name", name, *(["--margin", m] if m not in ("", "0") else []),
            *(["--rtstruct"] if rt else []))
    elif cmd == "margin":
        sel = Prompt.ask("structure")
        mm = Prompt.ask("mm (5 = expand, -3 = shrink, 5,5,10 = x,y,z)")
        rt = Confirm.ask("also write an RTSTRUCT (for the TPS)?", default=False)
        run(cmd, sel, "--mm", mm, *(["--rtstruct"] if rt else []))
    elif cmd == "labelmap":
        labels = Prompt.ask("labels in order, later wins (e.g. GTV=GTV* PTV=PTV)").split()
        run(cmd, *labels, "--name", Prompt.ask("file name", default="labels"))
    elif cmd == "crop":
        run(cmd, Prompt.ask("structure"), "--margin", Prompt.ask("margin mm", default="20"))
    elif cmd == "resample":
        run(cmd, "--spacing", Prompt.ask("spacing mm x,y,z", default="1,1,1"))
    else:
        run(cmd, Prompt.ask("structures", default="GTV*"))


def act_status():
    _, txt = offline_state()
    c.print(Panel(txt, title="network"))
    r = sh(["sudo", "-n", "/usr/local/sbin/orin-offline", "log"])
    c.print(Panel(r.stdout.strip() or "-", title="offline log (last entries)"))
    subprocess.run(["df", "-h", "/", VAULT])
    subprocess.run(["free", "-m"])


def act_quit():
    """Quitting always destroys the session space (key gone, data unrecoverable)."""
    if vault_unlocked():
        cases = list_cases()
        c.print(Panel(
            "Quitting [bold]destroys this session's encrypted space[/]: all cases and results in /data/medical "
            "are gone for good (nothing is kept on the Orin).\n"
            + (f"Cases: {', '.join(cases)}\n" if cases else "")
            + "Export what you need first (menu 6).", title="⚠ end of session", border_style="red"))
        if not Confirm.ask("Destroy the session space and quit?", default=False):
            return
        if not session_end():
            if Confirm.ask("Some files are still in use. Stop those programs and end anyway?", default=True):
                session_end(force=True)
        if vault_unlocked():
            c.print("[red]Session space could NOT be destroyed - it will be gone at the next reboot.[/]")
    elif model_running():
        run_script("llm-stop.sh")
    off, _ = offline_state()
    if off and Confirm.ask("Go back online (WiFi/Tailscale)?", default=False):
        act_offline(False)
    sys.exit(0)


def on_hangup(signum, frame):
    """Terminal closed / logout / kill: end the session space without asking."""
    try:
        if vault_unlocked():
            session_end(force=True)
    except Exception:
        pass
    finally:
        try:                                   # last resort: destroy directly, whatever failed above
            if vault_unlocked():
                sh(VAULT_CMD + ["end", "--force"])
        finally:
            os._exit(0)


IDLE = {"waiting": False, "since": time.time()}


def start_idle_watch():
    """Nobody at the keyboard for IDLE_MIN minutes while the console waits for input -> the session space is
    destroyed and the console exits (an unattended Orin must not keep patient data open). Long-running work
    (agent answering, tools running) does not count as idle: only waiting at a prompt does."""
    import threading
    from rich import prompt as _rp
    orig = _rp.PromptBase.get_input.__func__

    def get_input(cls, *a, **k):
        IDLE.update(waiting=True, since=time.time())
        try:
            return orig(cls, *a, **k)
        finally:
            IDLE.update(waiting=False, since=time.time())
    _rp.PromptBase.get_input = classmethod(get_input)

    def watch():
        while True:
            time.sleep(20)
            if IDLE["waiting"] and time.time() - IDLE["since"] > IDLE_MIN * 60 and vault_unlocked():
                c.print(f"\n[red]No input for {IDLE_MIN} min - destroying the session space.[/]")
                try:
                    session_end(force=True)
                finally:
                    os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def main():
    import signal
    start_idle_watch()
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, on_hangup)
    c.print("[bold]Orin offline console[/] - every session gets a new encrypted space; quitting destroys it.")
    session_start()
    while True:
        c.print()
        header()
        off, _ = offline_state()
        menu = [("1", "Go offline" if not off else "Go online again"),
                ("2", "Session space: active (destroyed on quit)" if vault_unlocked() else "Start a new session space"),
                ("3", "Import from USB drive  →  new case /data/medical/<case>/original"),
                ("4", "Ask the agent about a dataset"),
                ("5", "Run a tool directly (report / organize / anonymize / MHA / structures)"),
                ("6", "Export a result to USB drive"),
                ("7", "Status and offline log"),
                ("8", "Test mode: ON - switch off" if TEST["on"] else
                      "Test mode: work online with anonymized/synthetic data"),
                ("q", "Quit = destroy the session space")]
        for k, label in menu:
            c.print(f"  [cyan]{k}[/]  {label}")
        if not off and TEST["on"]:
            c.print("  [magenta]Test mode - online allowed. Anonymized or synthetic data only.[/]")
        elif not off:
            c.print("  [red]⚠ Online - go offline (1) before working with patient data, "
                    "or use test mode (8) for anonymized data.[/]")
        a = Prompt.ask("choice", default="")
        try:
            {"1": lambda: act_offline(not off), "2": act_vault, "3": act_import, "4": act_agent,
             "5": act_tool, "6": act_export, "7": act_status, "8": act_test_mode,
             "q": act_quit}.get(a, lambda: None)()
        except KeyboardInterrupt:
            c.print("\n[dim]cancelled[/]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
