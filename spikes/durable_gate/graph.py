"""Spike: a dev workflow that suspends on a human gate and resumes elsewhere.

The question Task 2 exists to answer:

    Can a workflow node block on a human decision, release its worker process
    entirely, and later resume in a *different* process with its state intact?

If the answer is no, the hard human-in-the-loop design in the roadmap does not
work and the gate has to hold a worker for the duration of the wait -- hours,
possibly overnight.

The graph deliberately mirrors the real shape: work, then a `design_review` gate
that wants *input* (comments) rather than a boolean, then more work whose
behaviour depends on what the human said.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class FlowState(TypedDict, total=False):
    task: str
    design: str
    review_decision: str
    review_comments: str
    build_log: list
    worker_trace: list


def _worker_id() -> str:
    return f"pid:{os.getpid()}"


def node_design(state: FlowState) -> dict:
    """Ordinary work before the gate. Runs in the first process."""
    trace = list(state.get("worker_trace") or [])
    trace.append(f"design@{_worker_id()}")
    return {
        "design": f"design for {state['task']}",
        "worker_trace": trace,
    }


def node_design_review(state: FlowState) -> dict:
    """The hard gate.

    `interrupt` raises out of the node. Control returns to whoever invoked the
    graph, the checkpointer has already persisted everything up to this point,
    and the process is free to exit. Nothing is held open.

    The payload is what a reviewer UI would render. It is an *input* gate, not an
    approve/reject one -- the response carries comments, matching the roadmap's
    ACP `elicitation` shape rather than `request_permission`.
    """
    response = interrupt(
        {
            "gate": "design_review",
            "kind": "input",
            "design": state.get("design"),
            "question": "Approve this design? Provide decision and comments.",
            "response_schema": {
                "decision": "approve | reject",
                "comments": "string",
            },
        }
    )
    trace = list(state.get("worker_trace") or [])
    trace.append(f"design_review@{_worker_id()}")
    return {
        "review_decision": response.get("decision", "unknown"),
        "review_comments": response.get("comments", ""),
        "worker_trace": trace,
    }


def node_build(state: FlowState) -> dict:
    """Work after the gate, whose behaviour depends on the human's answer.

    Proves the resumed run can actually *see* what the human supplied, rather
    than merely continuing past the gate.
    """
    trace = list(state.get("worker_trace") or [])
    trace.append(f"build@{_worker_id()}")
    log = list(state.get("build_log") or [])
    if state.get("review_decision") == "approve":
        log.append(f"built: {state.get('design')} (notes: {state.get('review_comments')})")
    else:
        log.append(f"halted: {state.get('review_comments')}")
    return {"build_log": log, "worker_trace": trace}


def build_graph(db_path: str):
    """Compile the graph against a SQLite checkpointer.

    check_same_thread=False because the connection is handed to the graph, which
    may touch it from another thread. The file on disk is what makes resumption
    in a different process possible at all.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    builder = StateGraph(FlowState)
    builder.add_node("design", node_design)
    builder.add_node("design_review", node_design_review)
    builder.add_node("build", node_build)
    builder.add_edge(START, "design")
    builder.add_edge("design", "design_review")
    builder.add_edge("design_review", "build")
    builder.add_edge("build", END)
    return builder.compile(checkpointer=SqliteSaver(conn))
