"""python toolset: the model writes Python, the user approves it, it runs in a sandbox.

params:
  folder      input data, mounted READ-ONLY at /data/input
  work        the only writable place, mounted at /data/work (default <folder>_work, created if missing)
  timeout     seconds per run (default 300)
  memory_mb   address-space limit (default 3000)

Sandbox (bubblewrap): no network, no access to the home folder / SSH keys / other data, own PID namespace,
/usr + the Python environment read-only, /tmp in memory, killed after `timeout`. If the sandbox cannot be
started the code is NOT run (fail closed).

Skills: approved code that worked can be saved (save_skill, needs approval) and re-run later without
writing it again (run_skill). Skills live in ~/orin-agent/skills/<name>.py - code only, never data.
"""
import datetime as dt
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys

from harness import Toolset, tool

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
SKILLS = os.path.join(REPO, "skills")
BWRAP = shutil.which("bwrap") or "/usr/bin/bwrap"
PYENV = sys.prefix                       # the venv we run in (~/dicom-env), mounted read-only
PY = os.path.join(PYENV, "bin", "python")
LIBS = "numpy, pydicom, SimpleITK, PIL (pillow); standard library"
PREAMBLE = ("import json as _j, os as _o\nINPUT = '/data/input'\nWORK = '/data/work'\n"
            "ARGS = _j.loads(_o.environ.get('SKILL_ARGS') or '{}')\n"
            "SERIES = _j.load(open('/data/series.json')) if _o.path.exists('/data/series.json') else {}\n"
            "MHA = '/data/mha'   # MHA export of this dataset (read-only), if export_mha has run\n"
            "import warnings as _w; _w.filterwarnings('ignore')\n"
            "exec(open('/data/lib/helpers.py').read())   # STRUCTURES, combine_masks, write_image, ...\n")
HELPERS = os.path.join(HERE, "helpers.py")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
CODE_BLOCK = re.compile(r"```(?:python|py)[ \t]*\n(.*?)```", re.S)   # closed blocks only (cut-off code is not run)


def _limits(mem_mb, cpu_s):
    def apply():
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 2**30, 8 * 2**30))      # one file <= 8 GB
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def sandbox_cmd(inp, work, index=None, mha=None):
    cmd = [BWRAP, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
           "--ro-bind", "/usr", "/usr",
           "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
           "--ro-bind", PYENV, PYENV,
           "--ro-bind", inp, "/data/input",
           "--bind", work, "/data/work",
           *(["--ro-bind", index, "/data/series.json"] if index else []),
           *(["--ro-bind", mha, "/data/mha"] if mha and os.path.isdir(mha) else []),
           "--ro-bind", HELPERS, "/data/lib/helpers.py",
           # the same folders also at their real paths: tool messages name real paths and the model copies them
           "--ro-bind", inp, inp, "--bind", work, work,
           *(["--ro-bind", mha, mha] if mha and os.path.isdir(mha) else []),
           "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
           "--chdir", "/data/work",
           "--setenv", "HOME", "/data/work", "--setenv", "PATH", "/usr/bin",
           "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "OMP_NUM_THREADS", "4",
           "--setenv", "LANG", "C.UTF-8"]
    if os.path.exists("/etc/ld.so.cache"):
        cmd += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
    if os.path.exists("/usr/lib64"):
        cmd += ["--symlink", "usr/lib64", "/lib64"]
    return cmd


def sandbox_works():
    try:
        r = subprocess.run([BWRAP, "--unshare-all", "--die-with-parent", "--ro-bind", "/usr", "/usr",
                            "--symlink", "usr/lib", "/lib", "--symlink", "usr/bin", "/bin", "--proc", "/proc",
                            "--dev", "/dev", "/bin/true"], capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def _snapshot(work):
    out = {}
    for d, _, fs in os.walk(work):
        for f in fs:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
                out[os.path.relpath(p, work)] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return out


def _human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def run_code(code, inp, work, timeout=300, memory_mb=3000, args=None, max_out=8000, index=None, mha=None):
    ok, why = sandbox_works()
    if not ok:
        return f"NOT RUN: sandbox (bubblewrap) does not work here ({why[:200]}). Run bin/setup-sandbox.sh once."
    before = _snapshot(work)
    env = {"SKILL_ARGS": json.dumps(args or {})}
    cmd = sandbox_cmd(inp, work, index, mha) + ["--setenv", "SKILL_ARGS", env["SKILL_ARGS"], PY, "-I", "-B", "-"]
    t0 = dt.datetime.now()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, start_new_session=True, preexec_fn=_limits(memory_mb, timeout))
    try:
        out, _ = p.communicate(PREAMBLE + code, timeout=timeout)
        status = f"exit code {p.returncode}"
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        status = f"KILLED after {timeout} s (time limit)"
    secs = (dt.datetime.now() - t0).total_seconds()
    if p.returncode and p.returncode < 0 and "KILLED" not in status:
        status += " (killed: memory or CPU limit?)"
    if "MemoryError" in (out or ""):
        status += f" - memory limit {memory_mb} MB"
    after = _snapshot(work)
    changed = sorted(k for k, v in after.items() if before.get(k) != v)
    out = out or ""
    if len(out) > max_out:
        out = out[:max_out // 2] + f"\n... [{len(out) - max_out} chars cut] ...\n" + out[-max_out // 2:]
    lines = [f"{status}, {secs:.1f} s", "--- output ---", out.rstrip() or "(no output)"]
    if changed:
        lines += [f"--- files written in /data/work ({len(changed)}) ---"] + \
                 [f"{k}  {_human(after[k][0])}" for k in changed[:40]] + \
                 ([f"... and {len(changed) - 40} more"] if len(changed) > 40 else [])
        lines += _geometry_warnings(work, changed, mha)
    return "\n".join(lines)


IMG_EXT = (".mha", ".mhd", ".nii", ".nii.gz", ".nrrd")


def _header(path):
    import SimpleITK as sitk
    r = sitk.ImageFileReader()
    r.SetFileName(path)
    r.ReadImageInformation()
    return r.GetSize(), r.GetSpacing(), r.GetOrigin(), r.GetNumberOfComponents()


def _geometry_warnings(work, changed, mha):
    """Images written by the script must sit on the grid of an exported CT/mask - otherwise they do not line up
    with the patient (typical small-model bug: numpy array written without the reference geometry)."""
    imgs = [k for k in changed if k.lower().endswith(IMG_EXT)]
    if not imgs or not mha or not os.path.isdir(mha):
        return []
    try:
        import numpy as np
        grids = []
        for d, _, fs in os.walk(mha):
            grids += [_header(os.path.join(d, f))[:3] for f in fs if f.endswith(".mha")]
        warn = []
        for k in imgs:
            size, sp, org, comp = _header(os.path.join(work, k))
            ok = comp == 1 and any(len(size) == len(s) and tuple(size) == tuple(s) and np.allclose(sp, gsp)
                                   and np.allclose(org, go) for s, gsp, go in grids)
            if not ok:
                warn.append(f"WARNING {k}: size={size} spacing={tuple(round(x, 3) for x in sp)} components={comp} "
                            "matches NO exported CT/mask grid - geometry lost, it will not line up with the "
                            "patient. Rewrite it with combine_masks()/write_image() (or CopyInformation).")
        return ["--- geometry check ---"] + warn if warn else ["--- geometry check: images are on the CT grid ---"]
    except Exception as e:
        return [f"--- geometry check skipped ({type(e).__name__}) ---"]


def _skill_meta(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    m = re.match(r'^"""(.*?)"""\n', src, re.S)
    head = m.group(1) if m else ""
    meta = {"description": "", "args": "", "saved": ""}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        if k.strip().lower() in meta:
            meta[k.strip().lower()] = v.strip()
    return meta, src[m.end():] if m else src


def setup(params):
    from toolsets.dicom import layout as L
    inp = L.input_dir(params["folder"])
    L.use_session_tmp(inp)
    if not os.path.isdir(inp):
        raise ValueError(f"not a folder: {inp}")
    work = os.path.abspath(os.path.expanduser(params.get("work") or L.out(inp, "work")))
    if work == inp or work.startswith(inp + os.sep):
        raise ValueError("work folder must be outside the input folder")
    mha_dir = os.path.abspath(os.path.expanduser(params.get("mha") or L.out(inp, "mha")))   # read-only at /data/mha
    timeout = int(params.get("timeout") or 300)
    mem = int(params.get("memory_mb") or 3000)
    state = {"ok": []}            # scripts that ran successfully in this session (#1, #2, ...)

    def ensure_work():
        os.makedirs(work, mode=0o700, exist_ok=True)

    def index_file():
        """SERIES inside the sandbox: {series_id: {modality, category, description, files: [paths sorted]}}
        - the same ids as list_series, so scripts and tools talk about the same series."""
        if state.get("index") and os.path.exists(state["index"]):
            return state["index"]
        from toolsets.dicom import LAST_SCAN
        if inp in LAST_SCAN:
            rows, files = LAST_SCAN[inp]
        else:
            from toolsets.dicom import scan as S
            res = S.scan(inp)
            _, rows, files = S.build_tree(res["instances"], show_phi=False)
        idx = {r["id"]: {"modality": r["modality"], "category": r["category"],
                         "description": r["series_description"],
                         "files": ["/data/input/" + p for p in files[r["id"]]]} for r in rows}
        import tempfile
        fd, path = tempfile.mkstemp(prefix="series_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        state["index"] = path
        return path

    @tool("run_python",
          "Write and run a Python script for a task no other tool covers. It runs in a sandbox: the dataset is "
          "READ-ONLY at INPUT ('/data/input'), write results ONLY under WORK ('/data/work'); no network. "
          f"Available: {LIBS}. INPUT, WORK, ARGS and SERIES are predefined. SERIES = {{series_id: {{'modality', "
          "'category', 'description', 'files': [paths sorted by slice]}}}} with the same ids as list_series - "
          "use it instead of walking INPUT (number of slices = len(files)). print() a short summary - the output "
          "comes back to you. Prefer pydicom.dcmread(path, stop_before_pixels=True) when pixels are not needed. "
          "The user reads and approves the code before it runs.",
          {"type": "object", "properties": {
              "purpose": {"type": "string", "description": "one sentence: what the script does"},
              "code": {"type": "string", "description": "complete Python script"}},
           "required": ["purpose", "code"]},
          read_only=False, deletes=lambda a: True)     # free code can also delete files in WORK -> always asked
    def run_python(code, purpose=""):
        ensure_work()
        res = run_code(code, inp, work, timeout, mem, index=index_file(), mha=mha_dir)
        if res.startswith("exit code 0") and "matches NO exported" not in res:   # broken images = not worked
            state["ok"].append({"code": code, "purpose": purpose, "wrote": "--- files written" in res})
            res += f"\n(worked - this is script #{len(state['ok'])}; can be kept with save_skill)"
        return res

    def pick_script(script=None):
        ok = state["ok"]
        if not ok:
            return None, "nothing to save: no script has run successfully in this session"
        if script:
            try:
                return ok[int(script) - 1], None
            except (ValueError, IndexError):
                return None, f"no script #{script}; there are {len(ok)} (#1..#{len(ok)})"
        # default: the last script that wrote files (not a later one that only displayed them), else the last one
        return next((s for s in reversed(ok) if s["wrote"]), ok[-1]), None

    @tool("list_skills", "List saved skills (scripts that worked before and were approved), with their arguments.")
    def list_skills():
        if not os.path.isdir(SKILLS):
            return "no skills saved yet"
        rows = []
        for f in sorted(os.listdir(SKILLS)):
            if f.endswith(".py"):
                meta, _ = _skill_meta(os.path.join(SKILLS, f))
                rows.append(f"{f[:-3]}: {meta['description']}" + (f"  ARGS: {meta['args']}" if meta["args"] else ""))
        return "\n".join(rows) or "no skills saved yet"

    @tool("save_skill",
          "Save a script that ran successfully as a reusable skill - you do NOT pass the code. `script` = its "
          "number (#N shown after each run); default: the last script that wrote files. Values that change between "
          "uses should be read from ARGS (a dict) - describe them in `args`. The user sees the code and approves.",
          {"type": "object", "properties": {
              "name": {"type": "string", "description": "lower_case_name"},
              "description": {"type": "string", "description": "what it does, one sentence"},
              "script": {"type": "integer", "description": "optional: script number #N"},
              "args": {"type": "string", "description": "optional: ARGS keys it reads, e.g. 'modality: str'"}},
           "required": ["name", "description"]},
          read_only=False, deletes=lambda a: True,     # the user reads the code that becomes a skill
          preview=lambda a: (pick_script(a.get("script"))[0] or {}).get("code", "(no script)"))
    def save_skill(name, description, args="", script=None):
        chosen, err = pick_script(script)
        if err:
            return err
        code = chosen["code"]
        if not NAME_RE.match(name or ""):
            return "name must be lower_case letters/digits/_ (3-40 chars)"
        os.makedirs(SKILLS, exist_ok=True)
        path = os.path.join(SKILLS, name + ".py")
        if os.path.exists(path):
            return f"a skill named {name} exists already - choose another name"
        clean = lambda s: re.sub(r'["\n]', " ", s)[:200]
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'"""\ndescription: {clean(description)}\nargs: {clean(args)}\n'
                    f'saved: {dt.datetime.now():%Y-%m-%d %H:%M}\n"""\n{code}\n')
        return f"saved skill {name} ({path})"

    @tool("run_skill",
          "Run a saved skill (see list_skills) in the same sandbox, with optional ARGS. Its code was approved when "
          "it was saved, so it runs without asking again.",
          {"type": "object", "properties": {
              "name": {"type": "string"},
              "args": {"type": "object", "description": "values for ARGS"}},
           "required": ["name"]},
          read_only=False, safe_when=lambda a: True)
    def run_skill(name, args=None):
        path = os.path.join(SKILLS, (name or "") + ".py")
        if not NAME_RE.match(name or "") or not os.path.isfile(path):
            return f"no skill named {name} (see list_skills)"
        _, code = _skill_meta(path)
        ensure_work()
        return run_code(code, inp, work, timeout, mem, args=args if isinstance(args, dict) else {}, index=index_file(), mha=mha_dir)

    def code_block(text):
        """A ```python block in the model's reply = run_python (code-as-action, as in smolagents / Qwen-Agent).
        Same approval and sandbox as the tool call; avoids escaping a whole script inside JSON."""
        m = CODE_BLOCK.search(text or "")
        if not m or not m.group(1).strip():
            return None
        code = m.group(1).rstrip()
        first = next((ln.strip("# ").strip() for ln in code.splitlines() if ln.strip().startswith("#")), "")
        return "run_python", {"purpose": first[:100] or "script from the reply", "code": code}

    return Toolset(name="python", tools=[run_python, list_skills, save_skill, run_skill],
                   describe=f"Python sandbox: input {inp} (read-only), work {work}", text_action=code_block)
