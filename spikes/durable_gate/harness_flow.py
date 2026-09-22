"""Spike, part 2: the same suspend/resume with a REAL agent-core OpenCode turn.

The roadmap asked for the proof to hold "with the harness in the loop". A gate
between two real model turns is the actual dev-flow shape, and it is where the
serialization boundary bites: a TurnResult carries live objects, so only its
extracted text may enter checkpointed state.
"""
from __future__ import annotations
import os, sqlite3
from typing import Any, TypedDict

from agent_core.config import HarnessConfig, use_config
from agent_core.harness import OpenCodeProcess, generate_opencode_config
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

BASE_URL = "http://token-pool.example/v1"
MODEL = "token-pool/gpt-5.5"


def harness_config(key: str) -> HarnessConfig:
    return HarnessConfig(
        opencode_model=MODEL,
        opencode_small_model=MODEL,
        opencode_provider="token-pool",
        opencode_provider_chain=f"token-pool:{MODEL}",
        opencode_provider_base_urls=f"token-pool.base_url={BASE_URL}",
        opencode_provider_tokens=f"token-pool.token={key}",
        opencode_turn_log_enabled=False,
        index_source_dirs="",
        opencode_external_dirs="",
    )


class HState(TypedDict, total=False):
    repo: str
    key: str
    proposal: str
    review_decision: str
    review_comments: str
    revision: str
    trace: list


def _turn(state: HState, prompt: str) -> str:
    """Run one real OpenCode turn. Returns TEXT ONLY.

    Returning the TurnResult itself would poison the checkpoint -- it is not
    msgpack serializable, and the failure surfaces at the next gate, far from
    the cause. Extract at the boundary.
    """
    with use_config(harness_config(state["key"])):
        # Writes opencode.json into the repo with the provider chain. Without it
        # opencode has no provider configured and the turn returns empty.
        generate_opencode_config(state["repo"])
        result = OpenCodeProcess().run_turn(
            prompt, session_id=None, repo_path=state["repo"], model_id=MODEL, timeout=180
        )
    text = (result.result or "").strip()
    if result.type != "completed" or not text:
        raise RuntimeError(f"turn failed: type={result.type} error={result.error}")
    return text


def node_propose(state: HState) -> dict:
    text = _turn(state, "In one short sentence, propose a design for a server-sent-events progress endpoint.")
    return {"proposal": text, "trace": [*(state.get("trace") or []), f"propose@pid:{os.getpid()}"]}


def node_design_review(state: HState) -> dict:
    response = interrupt({
        "gate": "design_review", "kind": "input",
        "proposal": state.get("proposal"),
        "question": "Approve this design? Provide decision and comments.",
    })
    return {
        "review_decision": response.get("decision", "unknown"),
        "review_comments": response.get("comments", ""),
        "trace": [*(state.get("trace") or []), f"design_review@pid:{os.getpid()}"],
    }


def node_revise(state: HState) -> dict:
    if state.get("review_decision") != "approve":
        return {"revision": "halted", "trace": [*(state.get("trace") or []), f"revise-skipped@pid:{os.getpid()}"]}
    text = _turn(state, f"A reviewer said: '{state.get('review_comments')}'. In one short sentence, restate the design addressing it.")
    return {"revision": text, "trace": [*(state.get("trace") or []), f"revise@pid:{os.getpid()}"]}


def build_harness_graph(db_path: str):
    b = StateGraph(HState)
    b.add_node("propose", node_propose)
    b.add_node("design_review", node_design_review)
    b.add_node("revise", node_revise)
    b.add_edge(START, "propose")
    b.add_edge("propose", "design_review")
    b.add_edge("design_review", "revise")
    b.add_edge("revise", END)
    return b.compile(checkpointer=SqliteSaver(sqlite3.connect(db_path, check_same_thread=False)))
