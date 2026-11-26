"""Policy: what the agent is allowed to do. Enforced by the harness, not by the model."""
from dataclasses import dataclass


@dataclass
class Policy:
    max_steps: int = 6              # model turns per question
    max_tool_calls: int = 12        # tool calls per question
    allow_write: bool = False       # tools with read_only=False are hidden unless True
    max_tool_output: int = 6000     # characters of tool text passed to the model
    max_context_chars: int = 18000  # messages only (tool schemas come on top); model context 12k tokens
    preview_on_question: bool = True  # "can you ...?" -> write tools only as a dry run, the user decides

    def allowed(self, tool):
        return tool.read_only or self.allow_write

    def clip(self, text):
        if len(text) > self.max_tool_output:
            return text[:self.max_tool_output] + f"\n... [truncated {len(text) - self.max_tool_output} chars]"
        return text
