"""The agent loop. Knows nothing about DICOM or any other domain."""
import importlib
import json
import re
import sys
import time

from .policy import Policy
from .tools import ToolResult

TOOLS_NOTE = re.compile(r"\n?\[tools used for this answer:[^\]\n]*\]")
# a measured value: number + unit (volume, dose, length, percent, HU)
MEASURE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:ml|mL|cc|cm³|c?Gy|mm|HU|%)(?![A-Za-z])")
SERIES_ID = re.compile(r"\bP\d{3}-S\d{2}-\d{2,3}\b")      # series ids made up by the model, not seen anywhere


def load_toolsets(names_with_params):
    """[{"name": "dicom", "params": {...}}, ...] -> list of Toolset."""
    sets = []
    for entry in names_with_params:
        mod = importlib.import_module(f"toolsets.{entry['name']}")
        sets.append(mod.setup(entry.get("params", {})))
    return sets


class Agent:
    def __init__(self, model, toolsets, system_prompt, policy=None, log=None, trace=None, confirm=None,
                 on_event=None):
        self.model = model
        # on_event(dict): live events for user interfaces
        #   {"type": "thinking"} / {"type": "tool_call", name, args} / {"type": "tool_result", name, text, images}
        #   {"type": "declined", name} / {"type": "answer", text}
        self.on_event = on_event or (lambda ev: None)
        # confirm(tool_name, args) -> bool; asked before any tool with read_only=False runs.
        # Default: refuse (a write tool can never run without an explicit confirm function).
        self.confirm = confirm or (lambda name, args: False)
        self.policy = policy or Policy()
        self.toolsets = toolsets
        self.tools = {}
        for ts in toolsets:
            for t in ts.tools:
                if not self.policy.allowed(t):
                    continue
                if t.name in self.tools:
                    raise ValueError(f"duplicate tool name {t.name}")
                self.tools[t.name] = t
        self.redactors = [ts.redact for ts in toolsets if ts.redact]
        self.history = [{"role": "system", "content": system_prompt}]
        self.log = log or (lambda s: print(s, file=sys.stderr))
        self.trace = trace            # optional file object: one JSON line per event

    # ------------------------------------------------------------ helpers
    def redact(self, text):
        for r in self.redactors:
            text = r(text)
        return text

    def _trace(self, **ev):
        if self.trace:
            ev["t"] = round(time.time(), 2)
            self.trace.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self.trace.flush()

    def _remember(self, question, answer, used):
        # the next question sees which tools ran (e.g. "save that script" after run_python)
        note = f"\n[tools used for this answer: {', '.join(used)}]" if used else ""   # see TOOLS_NOTE in ask()
        self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer + note}]
        self.history = self.history[:1] + self.history[1:][-8:]

    def _compact(self, msgs, keep_last=4, budget=None):
        """Keep the request inside the model's context: shorten old tool results and old tool-call
        arguments (e.g. earlier versions of a script), newest messages stay complete."""
        budget = budget or self.policy.max_context_chars

        def size():
            return sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)

        if size() <= budget:
            return
        for i, m in enumerate(msgs[1:-keep_last], 1):
            if m["role"] == "tool" and len(m["content"]) > 400:
                m["content"] = m["content"][:300] + "\n... [older output shortened]"
            elif m["role"] == "assistant" and m.get("tool_calls"):
                for c in m["tool_calls"]:
                    a = c["function"].get("arguments") or ""
                    if len(a) > 400:
                        try:
                            d = json.loads(a)
                            d = {k: (v if not isinstance(v, str) or len(v) < 200 else "[earlier version, omitted]")
                                 for k, v in d.items()}
                            c["function"]["arguments"] = json.dumps(d, ensure_ascii=False)
                        except json.JSONDecodeError:
                            c["function"]["arguments"] = "{}"
            if size() <= budget:
                return

    # ------------------------------------------------------------ loop
    def ask(self, question):
        msgs = self.history + [{"role": "user", "content": question}]
        schemas = [t.schema() for t in self.tools.values()]
        calls_made = 0
        seen, used = set(), []          # repeated identical calls are not run again; tools used -> history
        retries = 0
        nudged = False                  # one push when numbers appear without any tool call in this turn
        self._trace(event="question", text=question)
        for step in range(self.policy.max_steps):
            self.on_event({"type": "thinking"})
            self._compact(msgs)
            last = step == self.policy.max_steps - 1
            if last:                    # last step: no tools offered, the model has to answer now
                msgs.append({"role": "user", "content": "Step limit reached. Answer the question now with what "
                                                        "you found; say what is missing."})
            try:
                m = self.model.chat(msgs, None if last else schemas)
            except Exception as e:
                body = getattr(getattr(e, "response", None), "text", "") or ""
                if ("parse tool call" in body or "context" in body) and not last and retries < 2:
                    retries += 1        # broken/cut-off tool call or too long: shrink and ask for shorter code
                    self._compact(msgs, keep_last=2, budget=self.policy.max_context_chars // 2)
                    msgs.append({"role": "user", "content": "Your last tool call was cut off (too long or invalid "
                                                            "JSON). Use a SHORTER script, or answer now."})
                    continue
                msg = ("(stopped: the conversation became too long for the model's context - ask again, "
                       "more specifically)" if "context" in body else f"(stopped: model error: {e})")
                self._remember(question, msg, used)
                self.on_event({"type": "answer", "text": msg})
                return msg
            calls = [] if last else (m.get("tool_calls") or [])
            content = m.get("content") or ""
            # code-as-action (smolagents / Qwen-Agent style): a code block in the reply is run like a tool call,
            # so the model does not have to escape a whole script inside JSON
            text_call = None
            if not calls and not last and calls_made < self.policy.max_tool_calls:
                for ts in self.toolsets:
                    if ts.text_action:
                        text_call = ts.text_action(content)
                        if text_call and text_call[0] in self.tools:
                            break
                        text_call = None
            if text_call:
                calls_made += 1
                name, args = text_call
                text, images = self._dispatch(name, args, seen, used)
                msgs.append({"role": "assistant", "content": content})
                msgs.append({"role": "user", "content": f"Observation ({name}):\n{text}\n\n"
                                                        "Continue: fix and run again if needed, otherwise answer."})
                self._add_images(msgs, images)
                continue
            context = " ".join(str(x.get("content") or "") for x in msgs)
            new_ids = set(SERIES_ID.findall(content)) - set(SERIES_ID.findall(context))
            if not calls and not used and not last and not nudged and self.tools and \
                    (MEASURE.search(content) or new_ids):
                # numbers with units but no tool ran in this turn: small models invent values from the history
                # ("Fx6 GTVp 124 ml") - one push to look the value up, or to say it is not known
                nudged = True
                msgs.append({"role": "assistant", "content": content})
                msgs.append({"role": "user", "content": "Your answer gives measured values or series ids that you "
                                                        "did not look up in this turn. Get them with a tool now "
                                                        "(they must come from tool output), or say that you do "
                                                        "not know."})
                continue
            if not calls or calls_made >= self.policy.max_tool_calls:
                answer = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
                # the history note "[tools used ...]" is the harness's, not the model's: a model that copies it
                # invents tools it did not run - drop any such line from the answer
                answer = TOOLS_NOTE.sub("", answer).strip()
                answer = self.redact(answer) or "(no answer)"
                self._remember(question, answer, used)
                self._trace(event="answer", text=answer)
                self.on_event({"type": "answer", "text": answer})
                return answer
            msgs.append({"role": "assistant", "content": content, "tool_calls": calls})
            images = []
            for c in calls:
                calls_made += 1
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                text, imgs = self._dispatch(name, args, seen, used)
                msgs.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": text})
                images += imgs
            self._add_images(msgs, images)
        msg = "(stopped: step limit reached - try a more specific question)"
        self._remember(question, msg, used)
        self.on_event({"type": "answer", "text": msg})
        return msg

    def _dispatch(self, name, args, seen, used):
        """Run one tool call (approval, repeat guard, redaction). Returns (text for the model, images)."""
        self.log(f"  [tool] {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
        self.on_event({"type": "tool_call", "name": name, "args": args})
        t = self.tools.get(name)
        sig = (name, json.dumps(args, sort_keys=True))
        if sig in seen:
            # only blocks a repeat when nothing was written in between (a write may make a failed call work)
            res = ToolResult("REPEATED: exactly this call already ran in this answer and nothing has changed "
                             "since - read its result above (it may have been an error). Do not claim it "
                             "succeeded unless that result says so; fix the arguments or answer the user.")
        elif t is None:
            res = ToolResult(f"unknown tool {name}; available: {', '.join(self.tools)}")
        elif not t.read_only and not t.needs_no_confirm(args) and not self.confirm(
                name, {**args, "code": t.preview(args)} if t.preview else args):
            res = ToolResult("the user did NOT approve this action; nothing was changed. "
                             "Tell the user and do not retry unless asked.")
            self._trace(event="declined", name=name, args=args)
            self.on_event({"type": "declined", "name": name})
        else:
            if not t.read_only and not t.needs_no_confirm(args):
                seen.clear()            # a write changes the state: earlier calls may now give other results
            seen.add(sig)
            try:
                res = t.call(args)
            except Exception as e:
                res = ToolResult(f"tool error: {type(e).__name__}: {e}")
            first = (res.text or "").split("\n", 1)[0][:60]
            used.append(f"{name} ({first})" if not t.read_only else name)
        text = self.policy.clip(self.redact(res.text))
        self._trace(event="tool", name=name, args=args, result=text[:500], images=len(res.images))
        self.on_event({"type": "tool_result", "name": name, "text": text, "images": res.images})
        return text, [(name, img) for img in res.images]

    @staticmethod
    def _add_images(msgs, images):
        for name, img in images:   # tool messages are text-only -> images go in a user turn
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": f"[image returned by {name}]"},
                {"type": "image_url", "image_url": {"url": img}}]})
