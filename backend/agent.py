"""The agent loop: think -> act -> observe -> repeat, until done.

The model cannot execute anything. Each turn it emits JSON naming a tool and
its argument; this module runs the real Python function and pastes the result
back as the next observation. That is the entire mechanism.

Everything else here exists to stop a small local model from going in circles.
"""

import json
from dataclasses import dataclass, field

import agents as agent_specs
import config
import llm
import tools

# Flat schema on purpose. A nested oneOf per tool is more elegant and small
# models fail it constantly; one flat object with optional fields does not.
_ARG_FIELDS = {arg for spec in tools.TOOLS.values() for arg in spec["args"]}
AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {"type": "string", "enum": list(tools.TOOLS)},
        **{arg: {"type": "string"} for arg in sorted(_ARG_FIELDS)},
    },
    "required": ["thought", "tool"],
}

AGENT_SYSTEM = """You are the {label}. You answer questions about {subject}
by using tools, one step at a time.

{schema_card}

Tools available to you:
{tool_list}

How to work:
- Reply with JSON only: {{"thought": "...", "tool": "...", <the tool's field>}}
- One tool per reply. Never two.
- Look at the observations you already have before acting. Never repeat a tool
  call that you have already made.
- Use final_answer as soon as you can answer. Most questions need 1 or 2 tools.
- You have at most {max_steps} steps. If you are running out, answer with what
  you have.
- In final_answer, write the answer for the user in plain English. Do not show
  SQL and do not describe your steps.

Worked example for your domain (the numbers are step numbers, not output):
{examples}"""


@dataclass
class Step:
    thought: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    observation: str = ""
    error: bool = False


@dataclass
class AgentResult:
    answer: str
    steps: list = field(default_factory=list)
    stopped_early: bool = False

    @property
    def transcript(self) -> str:
        if not self.steps:
            return "(no steps)"
        out = []
        for i, s in enumerate(self.steps, 1):
            arg = next(iter(s.args.values()), "") if s.args else ""
            out.append(f"step {i}: {s.tool}({arg})")
            out.append(f"  thought: {s.thought}")
            out.append(f"  result: {s.observation}")
        return "\n".join(out)


class Agent:
    """An agent scoped to one data domain and its permitted tools."""

    def __init__(self, conn, searcher, schema_card, max_steps=None, spec=None):
        self.spec = spec or agent_specs.spec("cross")
        self.ctx = {
            "conn": conn,
            "searcher": searcher,
            "tables": self.spec["tables"],
            "tools": self.spec["tools"],
        }
        self.schema_card = schema_card
        self.max_steps = max_steps or config.AGENT_MAX_STEPS

    @property
    def label(self) -> str:
        return self.spec["label"]

    # ---------- prompt building ----------

    def _system(self) -> str:
        return AGENT_SYSTEM.format(
            label=self.spec["label"],
            subject=self.spec["subject"],
            schema_card=self.schema_card,
            tool_list=tools.describe_tools(self.spec["tools"]),
            examples=self.spec["examples"],
            max_steps=self.max_steps,
        )

    def _scratchpad(self, question: str, steps: list) -> str:
        """The model is stateless, so the whole story is resent every turn."""
        parts = [f"Question: {question}"]
        if steps:
            parts.append("\nWhat you have done so far:")
            for i, s in enumerate(steps, 1):
                arg = next(iter(s.args.values()), "") if s.args else ""
                parts.append(f"\n[step {i}] tool={s.tool} {arg}")
                parts.append(f"observation:\n{s.observation}")
        remaining = self.max_steps - len(steps)
        parts.append(f"\nSteps remaining: {remaining}. What is the next single step?")
        return "\n".join(parts)

    # ---------- the loop ----------

    def run(self, question: str) -> AgentResult:
        steps: list[Step] = []
        seen: set[tuple] = set()

        for _ in range(self.max_steps):
            raw = llm.complete(
                system=self._system(),
                user=self._scratchpad(question, steps),
                model=config.AGENT_MODEL,
                max_tokens=700,
                schema=AGENT_SCHEMA,
            )

            try:
                decision = json.loads(raw)
            except json.JSONDecodeError:
                steps.append(Step(
                    thought="", tool="(invalid)", observation=
                    "Your reply was not valid JSON. Reply with JSON only.", error=True))
                continue

            tool = (decision.get("tool") or "").strip()
            thought = (decision.get("thought") or "").strip()
            spec = tools.TOOLS.get(tool)
            args = {k: decision[k] for k in (spec["args"] if spec else []) if k in decision}

            if tool == "final_answer":
                answer = (decision.get("answer") or "").strip()
                if answer:
                    steps.append(Step(thought, tool, args, "(finished)"))
                    return AgentResult(answer, steps)
                steps.append(Step(thought, tool, args,
                                  "final_answer needs a non-empty 'answer' field.", True))
                continue

            # Loop guard: a confused small model will re-run the same call forever.
            fingerprint = (tool, json.dumps(args, sort_keys=True))
            if fingerprint in seen:
                steps.append(Step(thought, tool, args,
                                  "You already ran exactly this. Do something different, "
                                  "or use final_answer.", True))
                continue
            seen.add(fingerprint)

            try:
                observation = tools.call(tool, self.ctx, args)
                steps.append(Step(thought, tool, args, observation))
            except tools.ToolError as exc:
                steps.append(Step(thought, tool, args, str(exc), error=True))

        # Ran out of steps: answer from whatever was gathered rather than failing.
        return AgentResult(self._forced_answer(question, steps), steps, stopped_early=True)

    def _forced_answer(self, question: str, steps: list) -> str:
        evidence = "\n\n".join(
            f"{s.tool}:\n{s.observation}" for s in steps if not s.error
        ) or "(nothing was retrieved)"
        try:
            return llm.complete(
                system="Answer the question using only the evidence given. Be short. "
                       "If the evidence is not enough, say exactly what is missing.",
                user=f"Question: {question}\n\nEvidence:\n{evidence}",
                model=config.ANSWER_MODEL,
                max_tokens=600,
            )
        except Exception:
            return ("I could not finish this within the step limit. "
                    "Try asking it as two separate questions.")
