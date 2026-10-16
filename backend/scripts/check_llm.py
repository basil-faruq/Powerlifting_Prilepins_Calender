"""Measure whether the configured model can actually call a tool.

Run this before building anything on the agent. The planner's design leans
entirely on tool calling: the model is never allowed to compute a number, so
every fact it states has to come back from a tool it chose to call (CLAUDE.md
non-negotiable 1). A model that answers in prose instead of calling the tool is
not slightly worse at this job, it is unusable for it -- it will start inventing
weights, which is the one failure mode the whole architecture exists to prevent.

So this asks five questions that cannot be answered without the tool and counts
how many actually call it.

**Target: 5/5.** At 4/5 or below, move to a larger model before writing the
router on top of it; `qwen2.5:32b` is the next step up and
`.claude/specs/11-LLM_Provider_And_Embeddings.md` §10 lists the alternatives.
The weather tool is deliberately trivial so that a failure is about the model's
tool calling and nothing else.

It also does one embedding round-trip and prints the width, which is the
cheapest way to catch a model and an `EMBED_DIM` that disagree -- a mismatch
that otherwise surfaces much later, as a database error on the first note.

    uv run python -m scripts.check_llm
    uv run python -m scripts.check_llm --runs 10
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse

from planner_core.config import get_settings
from planner_core.errors import LlmUnavailable
from planner_core.llm import check_ollama_models, get_embedder, get_model

# Five different shapes of the same request, because a model that calls the
# tool for "what is the weather in Berlin?" may still answer "is it raining in
# Oslo?" from its own head. Tool calling has to survive the phrasing.
PROMPTS = (
    "What is the weather in Berlin?",
    "Is it raining in Oslo right now?",
    "I'm packing for Lisbon tomorrow - how warm is it there?",
    "Compare the weather in Munich and Hamburg.",
    "Tell me the current conditions for Reykjavik.",
)

TOOL_NAME = "get_weather"

# Pydantic AI prints an ASCII-art banner and a telemetry pitch on the first
# agent run, which buries the three lines this script exists to report. This is
# the library's own documented flag for it, so no environment variable is
# involved and `get_settings()` remains the only source of configuration.
pydantic_ai.BANNER_ENABLED = False


def build_agent() -> Agent[None, str]:
    agent: Agent[None, str] = Agent(
        get_model("agent"),
        instructions=(
            "You are a weather assistant. Use the get_weather tool for any "
            "question about current weather. Never guess the weather yourself."
        ),
    )

    @agent.tool_plain
    def get_weather(city: str) -> str:
        """Return the current weather for a city.

        Args:
            city: The city to report on.
        """
        # A fixed string: this script measures whether the tool gets called,
        # not what the model does with the answer.
        return f"{city}: 14C, overcast, wind 11 km/h"

    return agent


def called_tool(messages: object) -> bool:
    """Did any model turn in this run ask for the tool by name?

    Read off the message history rather than the final text, because a model
    can call the tool and then describe the weather badly, and that still counts
    -- the router only needs the call to happen.
    """
    if not isinstance(messages, list):
        return False
    return any(
        part.tool_name == TOOL_NAME
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.tool_calls
    )


async def run_once(agent: Agent[None, str], prompt: str) -> tuple[bool, str]:
    try:
        result = await agent.run(prompt)
    except Exception as exc:  # noqa: BLE001 - a probe reports failures, never raises
        return False, f"{type(exc).__name__}: {exc}"
    if called_tool(result.all_messages()):
        return True, "called get_weather"
    return False, f"answered without the tool: {result.output[:60]!r}"


async def check_tool_calling(runs: int) -> int:
    agent = build_agent()
    successes = 0
    for index, prompt in enumerate(_cycle(PROMPTS, runs), start=1):
        ok, detail = await run_once(agent, prompt)
        successes += ok
        print(f"  {index}. {'PASS' if ok else 'FAIL'}  {detail}")
    return successes


def _cycle(prompts: tuple[str, ...], runs: int) -> list[str]:
    return [prompts[i % len(prompts)] for i in range(runs)]


def check_embedding() -> int:
    """One round-trip. Returns the width, or 0 if it could not be measured."""
    settings = get_settings()
    try:
        vector = get_embedder().embed_query("knee pain")
    except LlmUnavailable as exc:
        print(f"  FAIL  {exc}")
        return 0
    except ValueError as exc:
        print(f"  FAIL  {exc}")
        return 0
    width = len(vector)
    verdict = "PASS" if width == settings.embed_dim else "FAIL"
    print(f"  {verdict}  {settings.ollama_embed_model} returned {width} floats")
    return width


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        default=len(PROMPTS),
        help="How many tool-calling attempts to make (default: 5).",
    )
    args = parser.parse_args()
    if args.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2

    settings = get_settings()
    agent_model = (
        settings.ollama_chat_model
        if settings.llm_provider == "ollama"
        else settings.anthropic_model
    )
    print(f"Provider: {settings.llm_provider}")
    print(f"Agent model: {agent_model}")
    print(f"Embedding model: {settings.ollama_embed_model} (always local)")
    # See the flush before the advice at the end: piped stdout is buffered and
    # stderr is not, so every stderr write needs the header out of the way.
    sys.stdout.flush()

    # Ask first, so a missing model is reported as a missing model. Measuring
    # anyway would score it 0/5 and send the reader off to install a *bigger*
    # model to fix a download that never happened.
    if settings.llm_provider == "ollama":
        try:
            missing = check_ollama_models()
        except LlmUnavailable as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
        if missing:
            print("\nThese models are configured but not pulled:", file=sys.stderr)
            for model in missing:
                print(f"  ollama pull {model}", file=sys.stderr)
            print("\nNothing can be measured until they are.", file=sys.stderr)
            return 2

    print("\nTool calling:")
    successes = asyncio.run(check_tool_calling(args.runs))

    print("\nEmbedding:")
    width = check_embedding()

    rate = successes / args.runs
    print(f"\nTool-call success rate: {successes}/{args.runs} ({rate:.0%})")
    print(f"Embedding dimension: {width or 'unavailable'} (EMBED_DIM={settings.embed_dim})")

    # stdout is block-buffered when piped while stderr is not, so without this
    # the advice below lands above the results it is advising about.
    sys.stdout.flush()

    if successes < args.runs:
        print(
            "\nBelow target. Every number the agent reports has to come from a tool, "
            "so unreliable tool calling means invented weights.\n"
            "Try a larger model (ollama pull qwen2.5:32b, then OLLAMA_CHAT_MODEL=qwen2.5:32b) "
            "before building the router on this one.",
            file=sys.stderr,
        )
        return 1
    if width != settings.embed_dim:
        print(
            f"\nEmbedding width does not match EMBED_DIM={settings.embed_dim}. "
            "Fix that before indexing any notes: the column is sized from this setting.",
            file=sys.stderr,
        )
        return 1

    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
