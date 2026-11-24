#!/usr/bin/env python3
"""
Web interface for the orin-agent harness.

  ~/dicom-env/bin/python web/server.py            # listens on 127.0.0.1:8090 only
Open from the PC through an SSH tunnel:  ssh -N -L 8090:127.0.0.1:8090 orin  ->  http://localhost:8090

Single user. Write actions wait for Approve/Decline in the page (5 min timeout = decline).
"""
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fastapi import FastAPI, HTTPException            # noqa: E402
from fastapi.responses import FileResponse, PlainTextResponse  # noqa: E402
from pydantic import BaseModel                          # noqa: E402
import uvicorn                                          # noqa: E402

from harness import Agent, Policy, load_toolsets, make_model  # noqa: E402
from toolsets.dicom import layout as L                         # noqa: E402

BROWSE_ROOTS = ["/data/medical", os.path.expanduser("~"), "/media", "/mnt"]
VAULT = "/data/medical"
REPORT_NAMES = ["summary.txt", "anonymization_report.txt", "export_report.txt", "manifest.csv"]
APPROVAL_TIMEOUT = 300

app = FastAPI(title="orin-agent")
SESSIONS = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------ helpers
def read_config(name):
    path = os.path.join(ROOT, "configs", os.path.basename(name))
    if not os.path.isfile(path):
        raise HTTPException(404, "config not found")
    return path, open(path, encoding="utf-8").read()


def placeholders(text):
    out = []
    for m in re.finditer(r"\$\{(\w+)(?::([^}]*))?\}", text):
        default = m.group(2) or ""
        if m.group(1) == "folder" and not default:
            default = VAULT                        # cases live in the encrypted vault: /data/medical/<case>
        out.append({"name": m.group(1), "required": m.group(2) is None, "default": default})
    return out


def in_vault(path):
    p = os.path.abspath(os.path.expanduser(path or ""))
    return p == VAULT or p.startswith(VAULT + "/")


def vault_locked_error(path):
    if in_vault(path) and not os.path.ismount(VAULT):
        raise HTTPException(423, "No session space - start one in the sidebar first.")


def fill(text, values):
    pat = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")
    missing = [m.group(1) for m in pat.finditer(text) if m.group(2) is None and not values.get(m.group(1))]
    if missing:
        raise HTTPException(400, f"missing: {', '.join(missing)}")
    return json.loads(pat.sub(lambda m: json.dumps(values.get(m.group(1)) or m.group(2) or "")[1:-1], text))


def inside_allowed(path):
    p = os.path.realpath(os.path.expanduser(path))
    return any(p == r or p.startswith(r.rstrip("/") + "/") for r in BROWSE_ROOTS), p


class Session:
    def __init__(self, cfg_name, values):
        _, text = read_config(cfg_name)
        cfg = fill(text, values)
        self.id = uuid.uuid4().hex[:12]
        self.config = cfg_name
        self.values = values
        self.events = []
        self.cond = threading.Condition()
        self.pending = {}            # approval id -> {"event": Event, "ok": bool}
        self.busy = False
        self.model = make_model(cfg["model"])
        self.toolsets = load_toolsets(cfg["toolsets"])
        prompt = open(os.path.join(ROOT, cfg["system_prompt"]), encoding="utf-8").read()
        self.agent = Agent(self.model, self.toolsets, prompt, Policy(**cfg.get("policy", {})),
                           log=lambda s: None, confirm=self.confirm, on_event=self.emit)
        self.show_images = bool(cfg.get("ui", {}).get("show_images", False))
        self.out_dirs = [v for k, v in values.items() if v] + \
            list(L.all_outputs(values["folder"]).values()) if values.get("folder") else []

    def emit(self, ev):
        ev = dict(ev)
        if ev.get("type") == "tool_result":
            imgs = ev.pop("images", []) or []
            ev["images"] = imgs if self.show_images else []
            ev["n_images"] = len(imgs)
            ev["text"] = ev.get("text", "")[:8000]
        with self.cond:
            ev["i"] = len(self.events)
            ev["t"] = time.time()
            self.events.append(ev)
            self.cond.notify_all()

    def confirm(self, name, args):
        aid = uuid.uuid4().hex[:8]
        slot = {"event": threading.Event(), "ok": False}
        self.pending[aid] = slot
        self.emit({"type": "approval", "id": aid, "name": name, "args": args})
        if not slot["event"].wait(APPROVAL_TIMEOUT):
            self.emit({"type": "approval_done", "id": aid, "ok": False, "reason": "timeout"})
            return False
        self.emit({"type": "approval_done", "id": aid, "ok": slot["ok"]})
        return slot["ok"]

    def ask(self, q):
        self.busy = True
        self.emit({"type": "question", "text": q})
        try:
            self.agent.ask(q)
        except Exception as e:
            self.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            self.busy = False
            self.emit({"type": "idle"})


# ------------------------------------------------------------------ API
class NewSession(BaseModel):
    config: str
    values: dict = {}


class Ask(BaseModel):
    question: str


class Approve(BaseModel):
    id: str
    ok: bool


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "index.html"))


@app.get("/api/configs")
def configs():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "configs", "*.json"))):
        text = open(p, encoding="utf-8").read()
        cfg = json.loads(re.sub(r"\$\{\w+(?::[^}]*)?\}", "", text))
        out.append({"name": os.path.basename(p), "placeholders": placeholders(text),
                    "toolsets": [t["name"] for t in cfg["toolsets"]],
                    "allow_write": cfg.get("policy", {}).get("allow_write", False)})
    return out


@app.get("/api/model")
def model_status():
    m = make_model({"url": "http://127.0.0.1:8080"})
    ok = m.health()
    return {"running": ok, "name": m.name() if ok else None}


@app.post("/api/model/{action}")
def model_action(action: str, size: str = "4b"):
    if action not in ("start", "stop") or size not in ("4b", "2b"):
        raise HTTPException(400, "bad action")
    script = os.path.join(ROOT, "bin", "llm-start.sh" if action == "start" else "llm-stop.sh")
    p = subprocess.run([script] + ([size] if action == "start" else []), capture_output=True, text=True, timeout=180)
    return {"ok": p.returncode == 0, "output": (p.stdout + p.stderr).strip()[-500:]}


@app.get("/api/vault")
def vault_status():
    if not os.path.exists("/usr/local/sbin/medical-vault"):
        return {"installed": False}
    unlocked = os.path.ismount(VAULT)
    info = {"installed": True, "unlocked": unlocked, "path": VAULT}
    if unlocked:
        import shutil
        u = shutil.disk_usage(VAULT)
        info.update(free_gb=round(u.free / 2**30), total_gb=round(u.total / 2**30))
    return info


@app.post("/api/vault/unlock")
def vault_start():
    """Start a new encrypted session space (random key, no passphrase)."""
    if os.path.ismount(VAULT):
        return {"ok": True, "output": "session space already active"}
    p = subprocess.run(["sudo", "-n", "/usr/local/sbin/medical-vault", "start"], capture_output=True, text=True,
                       timeout=300)
    return {"ok": p.returncode == 0 and os.path.ismount(VAULT), "output": (p.stdout + p.stderr).strip()[-400:]}


@app.post("/api/vault/lock")
def vault_end():
    """Destroy the session space (key gone, all data in it unrecoverable)."""
    with LOCK:
        busy = any(s.busy for s in SESSIONS.values())
    if busy:
        raise HTTPException(409, "agent is still working - wait until it is idle")
    with LOCK:
        SESSIONS.clear()             # close the agent session so no files stay open
    p = subprocess.run(["sudo", "-n", "/usr/local/sbin/medical-vault", "end"], capture_output=True, text=True,
                       timeout=120)
    return {"ok": p.returncode == 0, "output": (p.stdout + p.stderr).strip()[-400:]}


@app.get("/api/browse")
def browse(path: str = ""):
    if not path:
        path = VAULT
    vault_locked_error(path)
    ok, p = inside_allowed(path)
    if not ok or not os.path.isdir(p):
        raise HTTPException(403, "folder not allowed")
    dirs = sorted(d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d)) and not d.startswith("."))
    parent = os.path.dirname(p) if inside_allowed(os.path.dirname(p))[0] else None
    return {"path": p, "parent": parent, "dirs": dirs[:500]}


@app.post("/api/session")
def new_session(req: NewSession):
    if not make_model({"url": "http://127.0.0.1:8080"}).health():
        raise HTTPException(503, "model server not running - start it first")
    for k, v in req.values.items():               # folder and any output inside a locked vault
        if v:
            vault_locked_error(v)
    if "folder" in req.values:
        ok, p = inside_allowed(req.values["folder"])
        if not ok or not os.path.isdir(p):
            raise HTTPException(403, "folder not allowed or missing")
        req.values["folder"] = p
    try:
        s = Session(req.config, req.values)
    except SystemExit as e:
        raise HTTPException(400, str(e))
    with LOCK:
        SESSIONS.clear()          # single user: one active session
        SESSIONS[s.id] = s
    return {"id": s.id, "model": s.model.name(), "tools": [{"name": t.name, "write": not t.read_only}
                                                         for t in s.agent.tools.values()],
            "describe": [f"{ts.name}: {ts.describe}" for ts in s.toolsets if ts.describe]}


def get_session(sid):
    s = SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "session expired - load again")
    return s


@app.post("/api/session/{sid}/ask")
def ask(sid: str, req: Ask):
    s = get_session(sid)
    if s.busy:
        raise HTTPException(409, "still working on the previous question")
    threading.Thread(target=s.ask, args=(req.question.strip(),), daemon=True).start()
    return {"ok": True}


@app.get("/api/session/{sid}/events")
def events(sid: str, since: int = 0, wait: float = 20):
    s = get_session(sid)
    with s.cond:
        if len(s.events) <= since:
            s.cond.wait(timeout=min(wait, 25))
        return {"events": s.events[since:], "busy": s.busy}


@app.post("/api/session/{sid}/approve")
def approve(sid: str, req: Approve):
    s = get_session(sid)
    slot = s.pending.pop(req.id, None)
    if not slot:
        raise HTTPException(404, "no such approval (expired?)")
    slot["ok"] = req.ok
    slot["event"].set()
    return {"ok": True}


@app.get("/api/session/{sid}/reports")
def reports(sid: str):
    s = get_session(sid)
    found = []
    for d in s.out_dirs:
        for n in REPORT_NAMES:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                found.append({"path": p, "name": f"{os.path.basename(d)}/{n}",
                              "mtime": os.path.getmtime(p)})
    return sorted(found, key=lambda x: -x["mtime"])


@app.get("/api/session/{sid}/report", response_class=PlainTextResponse)
def report(sid: str, path: str):
    s = get_session(sid)
    allowed = {os.path.join(d, n) for d in s.out_dirs for n in REPORT_NAMES}
    if path not in allowed or not os.path.isfile(path):
        raise HTTPException(403, "not a report")
    return s.agent.redact(open(path, encoding="utf-8", errors="replace").read()[:200000])


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ORIN_UI_PORT", 8090)), log_level="warning")
