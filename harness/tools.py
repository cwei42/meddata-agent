"""Tool interface for the harness.

A toolset is a Python module (toolsets/<name>/__init__.py) that exposes

    def setup(params: dict) -> Toolset

The harness never imports anything domain-specific; it only sees Tool objects.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ToolResult:
    """What a tool returns: text for the model, optional images (data: URLs)."""
    text: str
    images: list = field(default_factory=list)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                      # JSON schema of the arguments
    fn: Callable[..., object]             # returns str or ToolResult
    read_only: bool = True
    # for write tools: args for which no confirmation is needed (e.g. a dry run)
    safe_when: Optional[Callable[[dict], bool]] = None
    # for write tools: extra text the user must see before approving (e.g. the code a skill will contain)
    preview: Optional[Callable[[dict], str]] = None
    # for write tools: args for which the call deletes or replaces existing data (always asked, see Policy)
    deletes: Optional[Callable[[dict], bool]] = None

    def needs_no_confirm(self, args):
        return bool(self.safe_when and self.safe_when(args))

    def needs_approval(self, args, confirm_writes=False):
        """True when the user must approve this call: deleting or replacing data always,
        other writes only with Policy.confirm_writes. Read-only tools and dry runs never."""
        if self.read_only or self.needs_no_confirm(args):
            return False
        return bool(confirm_writes or (self.deletes and self.deletes(args)))

    def schema(self):
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def call(self, args: dict) -> ToolResult:
        props = self.parameters.get("properties", {})
        clean = {k: v for k, v in args.items() if k in props and v not in (None, "")}
        out = self.fn(**clean)
        return out if isinstance(out, ToolResult) else ToolResult(str(out))


@dataclass
class Toolset:
    name: str
    tools: list
    redact: Optional[Callable[[str], str]] = None   # applied to tool output AND final answers
    describe: str = ""                              # one line shown at start (e.g. "33 patients, 54 series")
    # optional: turn plain reply text into a tool call, e.g. a ```python block -> ("run_python", {"code": ...})
    text_action: Optional[Callable[[str], Optional[tuple]]] = None


def tool(name, description, parameters=None, read_only=True, safe_when=None, preview=None, deletes=None):
    """Decorator: turn a function into a Tool. Use inside setup() so it can close over state."""
    def wrap(fn):
        return Tool(name=name, description=description,
                    parameters=parameters or {"type": "object", "properties": {}},
                    fn=fn, read_only=read_only, safe_when=safe_when, preview=preview, deletes=deletes)
    return wrap


def describe_call(name, args, width=100):
    """Readable text of a tool call for approval prompts: short args inline, code/long text as a numbered block."""
    inline, blocks = [], []
    for k, v in (args or {}).items():
        if isinstance(v, str) and ("\n" in v or len(v) > width):
            lines = v.rstrip("\n").split("\n")
            blocks.append(f"--- {k} ({len(lines)} lines) ---\n" +
                          "\n".join(f"{i:3}  {line}" for i, line in enumerate(lines, 1)))
        else:
            inline.append(f"{k}={v!r}")
    return "\n".join([f"{name}({', '.join(inline)})"] + blocks)
