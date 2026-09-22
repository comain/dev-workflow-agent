"""What the pipeline's document stages are, read off the flow.

The graph is already data (`flow.yaml`), and the UI needs the same facts the
graph does: which documents exist, in what order, and which gate reviews each
one. Spelling that out a second time in Python is how the two drift -- a stage
added to the YAML renders nowhere, or an artifact the graph writes is refused
by the allowlist. So the list is derived from the flow rather than restated.

A *stage* is the repeated shape: render a prompt, run a turn, persist the
document. Most stages end at a human gate; a stage whose document is a critique
of an earlier one does not -- it feeds the gate that follows it. Nodes outside
the shape (``prepare_workspace``) are not stages and have no entry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Tuple

from agent_core.workflow import WorkflowSpec

from dev_flow_agent.workflow import FLOW_PATH

#: The node implementation that marks a stage's persist step.
PERSIST_USES = "persist_doc"

#: Turns that are part of another step rather than a step of their own, mapped
#: to the step that owns them. A fix pass belongs to the review that asked for
#: it, and an escalation gate belongs to the work that escalated.
FOLDED_INTO = {
    "fix": "review",
    "escalate_review": "review",
    "escalate_build": "build",
    # Added when the gate itself was added, and missed: a gate whose node
    # belongs to no step leaves the page with nothing to mark and no form to
    # answer it with, so a run stopped here looked like a broken page.
    "escalate_enforcement": "enforce",
}


@dataclass(frozen=True)
class Stage:
    """One document the pipeline produces and gates on."""

    label: str
    artifact: str
    state_key: str
    write: str
    persist: str
    #: The human gate closing this stage, or "" when another stage follows it.
    gate: str
    #: For a critique stage, the label of the stage it reviews.
    reviews: str = ""
    #: The label the stage's turn reports progress under, from the flow.
    turn_label: str = ""

    @property
    def is_review(self) -> bool:
        return bool(self.reviews)


def _stages(spec: WorkflowSpec) -> Tuple[Stage, ...]:
    after = {source: target for source, target in spec.edges}
    before = {target: source for source, target in spec.edges}
    gates = {node.name for node in spec.nodes if node.uses == "human_gate"}
    turn_labels = {
        node.name: str(node.config.get("label") or "")
        for node in spec.nodes
        if node.uses == "agent_turn"
    }
    found = []
    for node in spec.nodes:
        if node.uses != PERSIST_USES:
            continue
        artifact = str(node.config.get("artifact") or "")
        if not artifact:
            raise ValueError(f"{node.name} needs config['artifact']")
        label = str(node.config.get("label") or artifact.removesuffix(".md"))
        found.append(
            Stage(
                label=label,
                artifact=artifact,
                state_key=str(node.config.get("state_key") or f"{label}_md"),
                write=before.get(node.name, ""),
                persist=node.name,
                # Only a human gate closes a stage. The node after a critique
                # stage is the next prompt, which is not one.
                gate=next_node if (next_node := after.get(node.name, "")) in gates else "",
                reviews=str(node.config.get("reviews") or ""),
                turn_label=str(node.config.get("turn_label") or "")
                or turn_labels.get(before.get(node.name, ""), ""),
            )
        )
    return tuple(found)


@lru_cache(maxsize=1)
def stages() -> Tuple[Stage, ...]:
    """The pipeline's document stages, in flow order."""
    return _stages(WorkflowSpec.from_file(FLOW_PATH))


def review_for(label: str) -> Optional[Stage]:
    """The critique stage covering ``label``, when the flow has one."""
    return next((s for s in stages() if s.reviews == label), None)


@dataclass(frozen=True)
class Step:
    """One position in the pipeline, as the page shows it."""

    node: str
    label: str
    #: The label progress reports under, or "" for a gate.
    turn_label: str = ""
    #: Whether this step's work fans out into branches that report under
    #: `<turn_label>_<branch>`. Declared, not inferred from the label: an
    #: underscore rule would give `design` the `design_review` step's events.
    fans_out: bool = False


@lru_cache(maxsize=1)
def pipeline_steps() -> Tuple[Step, ...]:
    """The pipeline as a reader follows it, taken from the flow's own order.

    Derived from the *turns*, not the documents. A strip built from documents
    cannot show building, reviewing or simplifying at all -- those stages write
    code rather than a document -- so for the whole unattended half it had
    nothing correct to point at.
    """
    spec = WorkflowSpec.from_file(FLOW_PATH)
    gate_labels = {
        stage.gate: f"review {stage.reviews or stage.label}"
        for stage in stages()
        if stage.gate
    }
    steps = []
    for node in spec.nodes:
        if node.name == spec.entry:
            steps.append(Step(node.name, "prepare"))
        elif node.uses == "agent_turn":
            label = str(node.config.get("label") or node.name)
            if label not in FOLDED_INTO:
                steps.append(Step(node.name, label.replace("_", " "), label))
        elif node.uses == PERSIST_USES and node.config.get("turn_label"):
            label = str(node.config["turn_label"])
            steps.append(
                Step(
                    node.name,
                    label.replace("_", " "),
                    label,
                    fans_out=bool(node.config.get("fans_out")),
                )
            )
        elif node.uses == "human_gate" and node.name in gate_labels:
            steps.append(Step(node.name, gate_labels[node.name]))
    return tuple(steps)


def step_for_node(node: str) -> Optional[Step]:
    """The step a graph node belongs to, folding the conditional ones in."""
    if not node:
        return None
    steps = pipeline_steps()
    exact = next((s for s in steps if s.node == node), None)
    if exact is not None:
        return exact
    owner = FOLDED_INTO.get(node)
    return next((s for s in steps if s.turn_label == owner), None) if owner else None


def step_for_turn(label: str) -> Optional[Step]:
    """The step whose turns report under ``label``."""
    if not label:
        return None
    steps = pipeline_steps()
    exact = next((s for s in steps if s.turn_label == label), None)
    if exact is not None:
        return exact
    owner = FOLDED_INTO.get(label)
    if owner:
        return next((s for s in steps if s.turn_label == owner), None)
    # A fanned-out step owns the labels its branches report under --
    # `review_security` belongs to `review`. Only a step that declares it,
    # or `design` would claim `design_review`.
    return next(
        (s for s in steps if s.fans_out and label.startswith(s.turn_label + "_")),
        None,
    )


def artifact_names() -> frozenset:
    """Documents the product will serve. The graph decides this, not a list."""
    return frozenset(stage.artifact for stage in stages())
