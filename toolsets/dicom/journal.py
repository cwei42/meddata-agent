"""
journal.py - Operation log and recycle bin of one case, so every step can be traced and undone.

    <case>/journal.jsonl   one JSON line per step: time, version, tool, arguments (names redacted), approval,
                           result, and which files the step created, changed, deleted or moved to the bin
    <case>/.trash/         recycle bin: earlier results replaced with overwrite=true, files removed by an undo,
                           the work/ folder as it was before free Python code ran

The originals (<case>/original/) are never changed, so undoing a step only has to deal with its own output.
Undo works like a stack: always the newest step that is not undone yet. It is started by the user in the console,
never by the model. Everything lives in the encrypted session space and is destroyed with it.
"""
import datetime as dt
import hashlib
import json
import os
import shutil

from . import layout as L

LOG = "journal.jsonl"
TRASH = ".trash"
SKIP_TOP = {L.ORIGINAL, TRASH, LOG}
HASH_LIMIT = 1 << 30              # hash created files only up to 1 GB per step (above: sizes only)
BACKUP_LIMIT = 2 << 30            # back up work/ before free code only up to 2 GB
BACKUP_BEFORE = {"run_python": "work"}   # tools that may change or delete files in place in that step folder
WRITE_KINDS = ("write", "cli")            # steps whose file changes are recorded (and can be undone)


def _now():
    return dt.datetime.now()


def _rel(case, p):
    return os.path.relpath(p, case)


def snapshot(case):
    """{relative path: (size, mtime_ns)} of all files of the case except originals, bin and log."""
    out = {}
    for top in sorted(os.listdir(case)) if os.path.isdir(case) else []:
        if top in SKIP_TOP:
            continue
        p = os.path.join(case, top)
        if os.path.isfile(p):
            st = os.stat(p)
            out[top] = (st.st_size, st.st_mtime_ns)
            continue
        for d, _, fs in os.walk(p):
            for f in fs:
                fp = os.path.join(d, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out[_rel(case, fp)] = (st.st_size, st.st_mtime_ns)
    return out


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _size(path):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def move_to_trash(case, path, label):
    """Move `path` (inside the case, never original/) into the bin instead of deleting it. Older bin entries
    with the same label are removed, so the bin keeps one earlier version per step. Returns the bin path."""
    case, path = os.path.abspath(case), os.path.abspath(path)
    orig = os.path.join(case, L.ORIGINAL)
    if os.path.commonpath([case, path]) != case or path == case or os.path.commonpath([orig, path]) == orig:
        raise ValueError(f"not a result folder of the case: {path}")
    trash = os.path.join(case, TRASH)
    os.makedirs(trash, exist_ok=True)
    for old in os.listdir(trash):
        if old.endswith("_" + label) and "_replaced_" in old:
            shutil.rmtree(os.path.join(trash, old), ignore_errors=True)
    dst = os.path.join(trash, f"{_now():%Y%m%d-%H%M%S-%f}_replaced_{label}")
    _move(path, dst)
    return dst


class Journal:
    """Operation log of one case. The harness calls begin()/end() around every tool call (see Agent.journal)."""

    def __init__(self, case, redact=None, source="agent"):
        self.case = os.path.abspath(case)
        self.path = os.path.join(self.case, LOG)
        self.trash = os.path.join(self.case, TRASH)
        self.redact = redact or (lambda s: s)
        self.source = source

    # ------------------------------------------------------------ writing
    def entries(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except OSError:
            return []

    def _next_id(self):
        e = self.entries()
        return (e[-1]["id"] + 1) if e else 1

    def _clean(self, v):
        if isinstance(v, str):
            return self.redact(v)
        if isinstance(v, dict):
            return {k: self._clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self._clean(x) for x in v]
        return v

    def _write(self, entry):
        os.makedirs(self.case, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _base(self, name, args, kind, approved=None):
        e = {"id": self._next_id(), "time": f"{_now():%Y-%m-%d %H:%M:%S}", "version": L.version(),
             "source": self.source, "tool": name, "kind": kind, "args": self._clean(dict(args or {}))}
        if approved is not None:
            e["approved"] = approved
        return e

    def note(self, name, args, kind, status, approved=None, result=""):
        """A step that changes nothing: read-only call, dry run, declined, only previewed, export ..."""
        e = self._base(name, args, kind, approved)
        e["status"] = status
        if result:
            e["result"] = self.redact(str(result))[:300]
        return self._write(e)

    def begin(self, name, args, kind, approved=None):
        if kind not in WRITE_KINDS:
            return {"name": name, "args": args, "kind": kind, "approved": approved}
        ctx = {"name": name, "args": args, "kind": kind, "approved": approved, "before": snapshot(self.case),
               "trash_before": set(os.listdir(self.trash)) if os.path.isdir(self.trash) else set()}
        step = BACKUP_BEFORE.get(name)
        if step:
            folder = os.path.join(self.case, step)
            if os.path.isdir(folder) and os.listdir(folder):
                if _size(folder) <= BACKUP_LIMIT:
                    dst = os.path.join(self.trash, f"{_now():%Y%m%d-%H%M%S-%f}_before_{step}")
                    shutil.copytree(folder, dst, symlinks=True)
                    ctx["backup"] = (step, dst)
                    ctx["trash_before"].add(os.path.basename(dst))
                else:
                    ctx["backup_skipped"] = f"{step}/ larger than {BACKUP_LIMIT >> 30} GB - not backed up"
        return ctx

    def end(self, ctx, status, result="", **extra):
        if ctx["kind"] not in WRITE_KINDS:
            return self.note(ctx["name"], ctx["args"], ctx["kind"], status, ctx.get("approved"),
                             result if ctx["kind"] == "dry_run" else "")
        e = self._base(ctx["name"], ctx["args"], ctx["kind"], ctx.get("approved"))
        e.update(extra)
        e["status"] = status
        if result:
            e["result"] = self.redact(str(result).split("\n", 1)[0])[:300]
        before, after = ctx["before"], snapshot(self.case)
        new_trash = sorted((set(os.listdir(self.trash)) if os.path.isdir(self.trash) else set())
                           - ctx["trash_before"])
        # a replaced result (overwrite=true) was moved to the bin as a whole: its old files are in the bin,
        # every file now in that folder is new - also where the new file has the same name as an old one
        moved = [{"from": t.split("_replaced_", 1)[1], "to": os.path.join(TRASH, t)}
                 for t in new_trash if "_replaced_" in t]
        in_moved = lambda p: any(p == m["from"] or p.startswith(m["from"] + "/") for m in moved)
        created = sorted(p for p in after if p not in before or in_moved(p))
        changed = sorted(p for p in set(after) & set(before) if after[p] != before[p] and not in_moved(p))
        deleted = sorted(p for p in set(before) - set(after) if not in_moved(p))
        total = sum(after[p][0] for p in created)
        e["created"] = [{"path": p, "size": after[p][0],
                         **({"sha256": _sha(os.path.join(self.case, p))} if total <= HASH_LIMIT else {})}
                        for p in created]
        if created and total > HASH_LIMIT:
            e["note"] = f"{total >> 20} MB created - no checksums (limit {HASH_LIMIT >> 20} MB)"
        if changed:
            e["changed"] = changed
        if deleted:
            e["deleted"] = deleted
        if moved:
            e["moved_to_trash"] = moved
        if "backup" in ctx:
            step, dst = ctx["backup"]
            touched = [p for p in changed + deleted if p == step or p.startswith(step + "/")]
            if touched:
                e["backup"] = {"folder": step, "to": _rel(self.case, dst)}
            else:                       # nothing changed in place: the copy is not needed
                shutil.rmtree(dst, ignore_errors=True)
        if ctx.get("backup_skipped"):
            e["note"] = (e.get("note", "") + "; " if e.get("note") else "") + ctx["backup_skipped"]
        return self._write(e)

    # ------------------------------------------------------------ undo
    def _undone(self):
        return {e["undoes"] for e in self.entries() if e.get("undoes")}

    def last_undoable(self):
        """Newest step that changed files and is not undone yet (undo goes back step by step)."""
        done = self._undone()
        for e in reversed(self.entries()):
            if e.get("undoes") or e["id"] in done:
                continue
            if e.get("kind") in ("write", "cli") and e.get("status") == "done" and \
                    any(e.get(k) for k in ("created", "changed", "deleted", "moved_to_trash")):
                return e
        return None

    def describe(self, e):
        parts = [f"#{e['id']} {e['time']} {e['tool']}"]
        if e.get("created"):
            parts.append(f"{len(e['created'])} files created")
        if e.get("moved_to_trash"):
            parts.append("replaced " + ", ".join(m["from"] for m in e["moved_to_trash"]) + " (earlier result in bin)")
        if e.get("changed"):
            parts.append(f"{len(e['changed'])} files changed")
        if e.get("deleted"):
            parts.append(f"{len(e['deleted'])} files deleted")
        return ", ".join(parts)

    def undo_last(self):
        """Undo the newest step. Files it created go to the bin (not deleted), a replaced result comes back,
        work/ is restored from its copy. Returns (ok, text); the undo itself is a new log entry."""
        e = self.last_undoable()
        if not e:
            return False, "nothing to undo"
        case = self.case
        problems = []
        back = e.get("backup")
        moved = e.get("moved_to_trash") or []
        for m in moved:
            if not os.path.isdir(os.path.join(case, m["to"])):
                problems.append(f"earlier result of {m['from']} is no longer in the bin ({m['to']})")
        if back and not os.path.isdir(os.path.join(case, back["to"])):
            problems.append(f"copy of {back['folder']}/ is no longer in the bin")
        if not back:
            lost = [p for p in e.get("changed", []) + e.get("deleted", [])]
            if lost:
                problems.append(f"{len(lost)} files were changed or deleted in place without a copy - "
                                "they cannot be restored")
        for c in e.get("created", []):
            p = os.path.join(case, c["path"])
            if os.path.isfile(p) and os.path.getsize(p) != c["size"]:
                problems.append(f"{c['path']} was changed after step #{e['id']}")
        if problems:
            return False, f"step #{e['id']} ({e['tool']}) cannot be undone safely:\n- " + "\n- ".join(problems)

        ctx = self.begin("undo", {"step": e["id"], "tool": e["tool"]}, "write")
        bin_dir = os.path.join(self.trash, f"{_now():%Y%m%d-%H%M%S-%f}_undo_{e['id']}")
        ctx["trash_before"].add(os.path.basename(bin_dir))
        skip = back["folder"] + "/" if back else None
        for c in e.get("created", []):
            if skip and c["path"].startswith(skip):
                continue
            p = os.path.join(case, c["path"])
            if os.path.exists(p):
                _move(p, os.path.join(bin_dir, c["path"]))
        # empty folders left behind by the removed files
        for c in e.get("created", []):
            d = os.path.dirname(os.path.join(case, c["path"]))
            while d != case and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
        for m in moved:
            dst = os.path.join(case, m["from"])
            if os.path.exists(dst):
                _move(dst, os.path.join(bin_dir, m["from"]))
            _move(os.path.join(case, m["to"]), dst)
        if back:
            folder = os.path.join(case, back["folder"])
            if os.path.exists(folder):
                _move(folder, os.path.join(bin_dir, back["folder"]))
            _move(os.path.join(case, back["to"]), folder)
        self.end(ctx, "done", f"undid step #{e['id']} ({e['tool']})", undoes=e["id"])
        removed = len([c for c in e.get("created", []) if not (skip and c["path"].startswith(skip))])
        msg = [f"undid step #{e['id']} ({e['tool']})"]
        if removed:
            msg.append(f"{removed} created files moved to the bin ({_rel(case, bin_dir)})")
        for m in moved:
            msg.append(f"earlier result of {m['from']}/ restored")
        if back:
            msg.append(f"{back['folder']}/ restored to its state before the step")
        return True, "\n".join(msg)

    # ------------------------------------------------------------ reading
    def summary(self, last=20, kinds=None):
        """Readable list of the newest steps (for the model and the console)."""
        done = self._undone()
        out = []
        for e in self.entries()[-last:]:
            if kinds and e.get("kind") not in kinds:
                continue
            line = f"#{e['id']} {e['time']} [{e.get('source', '')}] {e['tool']} ({e.get('kind')}, {e.get('status')})"
            if e.get("approved") is not None:
                line += " approved" if e["approved"] else " NOT approved"
            if e.get("undoes"):
                line += f" -> undid #{e['undoes']}"
            if e["id"] in done:
                line += " [undone]"
            args = {k: v for k, v in (e.get("args") or {}).items() if k != "code"}
            if args:
                line += " " + json.dumps(args, ensure_ascii=False)[:160]
            if "code" in (e.get("args") or {}):
                line += f" (code: {len(e['args']['code'].splitlines())} lines)"
            ch = []
            if e.get("created"):
                tops = sorted({c["path"].split("/")[0] for c in e["created"]})
                ch.append(f"{len(e['created'])} files created in {', '.join(tops)}")
            for m in e.get("moved_to_trash", []):
                ch.append(f"earlier {m['from']}/ moved to bin")
            if e.get("changed"):
                ch.append(f"{len(e['changed'])} changed")
            if e.get("deleted"):
                ch.append(f"{len(e['deleted'])} deleted")
            if ch:
                line += "\n    " + "; ".join(ch)
            if e.get("result"):
                line += "\n    result: " + e["result"][:160]
            out.append(line)
        return "\n".join(out) or "the log is empty"
