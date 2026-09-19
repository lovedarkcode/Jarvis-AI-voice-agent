"""
The agentic loop — how JARVIS handles an instruction nobody wrote code for.

The live Gemini session is a good router but a poor planner: it answers in one
breath, and a multi-step job ("find every invoice from March, total them, and
put the number in a spreadsheet") needs to see the result of step one before it
can choose step two. That is what this module is. `agent_task` hands a goal to a
local planner model which then drives core/primitives.py in a loop — act,
observe, decide again — until the goal is met or the step budget runs out.

This is the piece that makes the file-per-task pattern unnecessary. The old
design answered "can JARVIS do X?" with "only if someone wrote actions/x.py".
Here the answer is "if it can be done with a shell, a browser and a Python
interpreter, yes" — which is very nearly everything a computer can do.

core/prompt.txt referenced an `agent_task` tool for a long time before this
existed; the model was being told to call something that was never registered.
That is now a real tool.
"""
from __future__ import annotations

import json
import re
import traceback
from typing import Callable

from core.llm_client import call_llm_text
from core.mcp_gateway import TOOL as MCP_TOOL
from core.primitives import PRIMITIVES

# The planner drives the same toolset the live session does, MCP included —
# a multi-step goal is exactly where a logged-in browser earns its keep.
_TOOLSET = PRIMITIVES + [MCP_TOOL]

MAX_STEPS = 12

_PLANNER_SYSTEM = """You are the execution planner inside JARVIS, an assistant that controls a Windows computer.

You are given a GOAL and a transcript of the steps taken so far. Decide the SINGLE next step.

Reply with ONLY a JSON object, no prose, no markdown fence:

  {"tool": "<tool name>", "parameters": {...}, "why": "<short reason>"}

or, when the goal is achieved (or is impossible):

  {"done": true, "answer": "<what to tell the user, one or two sentences>"}

Available tools:
%s

Rules:
- One step at a time. Look at the previous step's OUTPUT before choosing the next.
- Prefer run_python for anything computational, structural, or unanticipated.
  You may install packages with shell ("pip install x") if an import fails.
- If a step errored, diagnose it from the traceback and try a corrected step.
  Do not repeat a failing step unchanged.
- The mcp tool reaches external services. You do not know its servers or their
  tool names from this list — call mcp with action='list' first when you need one.
- Stop as soon as the goal is met. Do not pad with extra verification steps.
- "answer" is spoken aloud to the user, so keep it plain and short.
"""


def _tool_catalog() -> str:
    """The planner's view of what it can do. Built from the same declarations the
    live session gets, so the two can never drift apart."""
    lines = []
    for spec in _TOOLSET:
        props = spec["parameters"].get("properties", {})
        args = ", ".join(props.keys())
        lines.append(f"- {spec['name']}({args}): {spec['description'].strip()}")
    return "\n".join(lines)


_HANDLERS = {spec["name"]: spec["handler"] for spec in _TOOLSET}


def _parse_step(raw: str) -> dict | None:
    """Pull the JSON object out of a planner reply. Models wrap JSON in fences
    and prose often enough that insisting on clean output would fail more than
    it would protect."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    # strict=False is load-bearing, not defensive. Every run_python step carries
    # multi-line source inside a JSON string, and planner models routinely emit
    # that as a real newline rather than the \n escape strict JSON demands.
    # Without this the loop rejects its own most common step and burns the whole
    # budget retrying.
    try:
        return json.loads(text, strict=False)
    except Exception:
        pass
    # Fall back to the first balanced {...} span in the reply.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1], strict=False)
                except Exception:
                    return None
    return None


def agent_task(parameters: dict, player=None, speak: Callable | None = None,
               session_memory=None, **_) -> str:
    goal = (parameters.get("goal") or parameters.get("task") or "").strip()
    if not goal:
        return "No goal supplied."

    budget = int(parameters.get("max_steps") or MAX_STEPS)
    system = _PLANNER_SYSTEM % _tool_catalog()
    transcript: list[str] = []

    _log(f"goal: {goal}", player)

    for step in range(1, budget + 1):
        prompt = f"GOAL: {goal}\n\n"
        prompt += "STEPS SO FAR:\n" + ("\n".join(transcript) if transcript else "(none yet)")
        prompt += f"\n\nChoose step {step}."

        try:
            raw = call_llm_text(prompt, system=system, timeout=90)
        except Exception as e:
            # Work already done is not undone by the planner dropping out, and
            # reporting only the error would tell the user nothing happened when
            # several steps may have succeeded.
            if transcript:
                done = "; ".join(t.splitlines()[0] for t in transcript)
                return (f"I got partway before the planner became unavailable ({e}). "
                        f"Completed: {done}")
            return f"Planner unavailable: {e}"

        plan = _parse_step(raw)
        if plan is None:
            transcript.append(f"[{step}] planner returned unparseable output; retrying")
            continue

        if plan.get("done"):
            answer = (plan.get("answer") or "Done.").strip()
            _log(f"done in {step - 1} steps", player)
            return answer

        name = plan.get("tool")
        args = plan.get("parameters") or {}
        handler = _HANDLERS.get(name)

        if handler is None:
            transcript.append(f"[{step}] ERROR: no such tool '{name}'. Available: {', '.join(_HANDLERS)}")
            continue

        # Keep the user company: a multi-step job is exactly the case where
        # silence reads as a freeze.
        why = (plan.get("why") or "").strip()
        _log(f"step {step}: {name} — {why}", player)
        if speak and why and step == 1:
            try:
                speak(why)
            except Exception:
                pass

        try:
            output = handler(parameters=args, player=player, session_memory=session_memory)
        except Exception:
            output = f"ERROR:\n{traceback.format_exc()}"

        output = str(output or "")
        if len(output) > 2000:
            output = output[:2000] + " ... [truncated]"
        transcript.append(f"[{step}] {name}({json.dumps(args, ensure_ascii=False)[:300]})\nOUTPUT: {output}")

    return (
        f"I worked through {budget} steps on that without reaching a clean finish. "
        f"Last state: {transcript[-1][:300] if transcript else 'nothing ran'}"
    )


def _log(message: str, player=None) -> None:
    print(f"[Agent] {message}")
    if player:
        try:
            player.write_log(f"SYS: agent — {message}")
        except Exception:
            pass


TOOL = {
    "name": "agent_task",
    "description": (
        "Carry out a multi-step task autonomously. Give it a goal in plain language and it "
        "plans, runs the primitives, reads each result, and adapts until the goal is met. "
        "Use this when a request needs several dependent steps, when the outcome of one step "
        "decides the next, or when no single tool covers what was asked. For a one-shot "
        "action (open an app, nudge the volume, run one snippet) call that primitive directly "
        "instead — this loop costs several model round trips."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The task in plain language, stated fully enough to act on without follow-up questions.",
            },
            "max_steps": {
                "type": "NUMBER",
                "description": "Step budget before giving up (default 12).",
            },
        },
        "required": ["goal"],
    },
    "handler": agent_task,
}
