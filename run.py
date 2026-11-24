#!/usr/bin/env python3
"""
run.py - start an agent from a config file.

  python run.py configs/dicom.json --set folder=/path/to/dicom            # interactive
  python run.py configs/dicom.json --set folder=/path -q "question"       # one question
  python run.py configs/system.json -q "Wie warm ist die GPU?"
  python run.py configs/dicom.json --set folder=/path --toolset system    # add another toolset
  python run.py configs/dicom.json --set folder=/path --model-url http://127.0.0.1:8081   # other model

${name} placeholders in the config are filled from --set name=value.
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from harness import Agent, Policy, describe_call, load_toolsets, make_model  # noqa: E402


def load_config(path, values):
    """${name} must be given with --set name=value; ${name:default} is optional."""
    text = open(path, encoding="utf-8").read()
    pat = re.compile(r"\$\{(\w+)(?::([^}]*))?\}")
    missing = sorted({m.group(1) for m in pat.finditer(text) if m.group(2) is None and m.group(1) not in values})
    if missing:
        sys.exit(f"config needs: {', '.join('--set ' + m + '=...' for m in missing)}")
    text = pat.sub(lambda m: json.dumps(values.get(m.group(1), m.group(2) or ""))[1:-1], text)
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser(description="Local agent harness")
    ap.add_argument("config")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="fill ${NAME} in config")
    ap.add_argument("-q", "--question")
    ap.add_argument("--toolset", action="append", default=[], help="add a toolset (no params)")
    ap.add_argument("--model-url", help="override model url")
    ap.add_argument("--trace", help="write a JSONL trace of questions/tool calls/answers to this file")
    ap.add_argument("--quiet", action="store_true", help="do not print tool calls")
    ap.add_argument("--yes", action="store_true", help="approve write actions without asking (careful)")
    args = ap.parse_args()

    values = dict(kv.split("=", 1) for kv in args.set)
    cfg = load_config(args.config, values)
    if args.model_url:
        cfg["model"]["url"] = args.model_url
    cfg["toolsets"] += [{"name": n} for n in args.toolset]

    model = make_model(cfg["model"])
    if not model.health():
        sys.exit(f"model server not reachable at {cfg['model']['url']} - start it first (llm-start.sh)")
    toolsets = load_toolsets(cfg["toolsets"])
    prompt = open(os.path.join(HERE, cfg["system_prompt"]), encoding="utf-8").read()
    trace = open(args.trace, "a", encoding="utf-8") if args.trace else None

    def confirm(name, targs):
        if args.yes and "code" not in targs:          # code is never auto-approved: a person must read it
            print(f"  [approve] {name}({targs}) auto-approved (--yes)", file=sys.stderr)
            return True
        try:
            with open("/dev/tty") as tty:
                print(f"\n  The agent wants to run a WRITE action:\n{describe_call(name, targs)}", file=sys.stderr)
                print("  Approve? [y/N] ", end="", file=sys.stderr, flush=True)
                return tty.readline().strip().lower() in ("y", "yes", "j", "ja")
        except OSError:
            print(f"  [declined] {name}: no terminal to ask for approval (use --yes)", file=sys.stderr)
            return False

    agent = Agent(model, toolsets, prompt, Policy(**cfg.get("policy", {})), trace=trace,
                  log=(lambda s: None) if args.quiet else None, confirm=confirm)

    print(f"model: {model.name()}  |  tools: {', '.join(agent.tools)}", file=sys.stderr)
    for ts in toolsets:
        if ts.describe:
            print(f"  {ts.name}: {ts.describe}", file=sys.stderr)

    if args.question:
        print(agent.ask(args.question))
        return
    print("\nAsk a question (empty line or 'exit' to quit).", file=sys.stderr)
    while True:
        try:
            q = input("\n? ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("exit", "quit", "q"):
            break
        print("\n" + agent.ask(q))


if __name__ == "__main__":
    main()
