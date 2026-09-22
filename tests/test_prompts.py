"""The prompts carry their rubric rather than naming a skill to go and read.

The agent runs with no skills installed, so an instruction to "use the
design-review skill" is inert: the model improvises a review and the checklist
the project actually reviews against never gets applied. The rubrics are
incorporated from dev-skills into the templates instead.
"""

from __future__ import annotations

import re

import pytest
from agent_core.prompts import PromptLibrary
from agent_core.workflow import WorkflowSpec

from dev_flow_agent.workflow import FLOW_PATH, PROMPT_DIR

#: Phrasing that sends the agent to fetch something that is not there.
DELEGATION = re.compile(
    r"(use|run|follow|apply|invoke)\s+(the\s+)?[`\w-]*\s*skill", re.IGNORECASE
)


#: Every value any template may reference, so rendering one is not a guessing
#: game about which stage it belongs to.
VALUES = {
    "title": "t",
    "request": "r",
    "comments": "",
    "triage_md": "i",
    "spec_md": "s",
    "design_md": "d",
    "plan_md": "p",
    "review_md": "v",
    "task_seq": 1,
    "task_title": "Add it",
    "task_body": "body",
    "jira": "TICKET-100",
    "base": "3d99d97",
    "trunk": "origin/master",
    "task_origin": "plan",
}


def _templates():
    spec = WorkflowSpec.from_file(FLOW_PATH)
    return sorted(
        {
            str(node.config["template"])
            for node in spec.nodes
            if node.uses == "render_prompt" and node.config.get("template")
        }
    )


def test_every_render_node_names_a_template_that_exists():
    library = PromptLibrary(PROMPT_DIR)
    for name in _templates():
        assert library.render(name, VALUES).strip()


@pytest.mark.parametrize("name", _templates())
def test_no_prompt_delegates_to_an_uninstalled_skill(name):
    body = (PROMPT_DIR / name).read_text(encoding="utf-8")
    # Provenance lives in a Jinja comment, which is not rendered to the model.
    rendered = PromptLibrary(PROMPT_DIR).render(name, VALUES)
    assert DELEGATION.search(rendered) is None, f"{name} delegates to a skill"
    assert "{#" in body, f"{name} should cite where its rubric came from"


def test_the_review_prompt_carries_the_axes_and_the_severities():
    rendered = PromptLibrary(PROMPT_DIR).render("review_design.md.j2", VALUES)
    for axis in (
        "Change scope",
        "Verification depth",
        "Decisiveness",
        "Reuse over rebuild",
        "Performance budget",
    ):
        assert axis in rendered
    for severity in ("Critical", "Important", "Nice-to-have"):
        assert severity in rendered
    assert "Do not decide anything" in rendered


def test_the_spec_and_design_prompts_carry_their_structure():
    library = PromptLibrary(PROMPT_DIR)
    spec = library.render("write_spec.md.j2", VALUES)
    for section in ("## Objective", "## Success Criteria", "## Boundaries", "## Open Questions"):
        assert section in spec

    design = library.render("write_design.md.j2", VALUES)
    for section in ("## Goals And Non-Goals", "## Failure-Mode Handling", "## Verification Plan"):
        assert section in design
    assert "Be decisive" in design


def _document_templates():
    return [
        name
        for name in _templates()
        if "_document_contract.md" in (PROMPT_DIR / name).read_text(encoding="utf-8")
    ]


def test_the_output_contract_is_stated_once_for_every_document_prompt():
    """Five prompts repeated the same three sentences. The contract is one
    partial now, so a change to it cannot land in four templates and miss one.
    """
    library = PromptLibrary(PROMPT_DIR)
    contract = "Your entire reply must be that markdown and nothing else"
    assert _document_templates(), "no prompt writes a document"
    for name in _templates():
        body = (PROMPT_DIR / name).read_text(encoding="utf-8")
        assert contract not in body, f"{name} restates the contract instead of including it"
        rendered = library.render(name, VALUES)
        expected = 1 if name in _document_templates() else 0
        assert rendered.count(contract) == expected


def test_each_document_prompt_names_the_document_it_must_write():
    library = PromptLibrary(PROMPT_DIR)
    expected = {
        "triage.md.j2": "triage.md",
        "write_spec.md.j2": "spec.md",
        "write_design.md.j2": "design.md",
        "review_design.md.j2": "design-review.md",
        "write_plan.md.j2": "plan.md",
        "enforce.md.j2": "enforcement.md",
    }
    assert set(_document_templates()) == set(expected)
    for name, document in expected.items():
        rendered = library.render(name, VALUES)
        assert f"Also write the same bytes to `{document}`" in rendered


def test_the_work_prompts_carry_their_build_rubric():
    """Build, fix and simplify change code rather than writing a document, and
    each has to bring the skill's rubric with it."""
    library = PromptLibrary(PROMPT_DIR)
    build = library.render("build_task.md.j2", VALUES)
    for cue in ("RED", "GREEN", "REFACTOR", "Simplicity first", "Scope discipline"):
        assert cue in build

    simplify = library.render("simplify_code.md.j2", VALUES)
    assert "Preserve behaviour exactly" in simplify
    assert "Understand before touching" in simplify

    # The review is one prompt per axis now, with the rubric supplied per axis.
    from dev_flow_agent.workflow.review import AXES, API_AXIS

    axis_prompt = library.render(
        "review_axis.md.j2",
        {**VALUES, "axis_title": "Security", "axis_focus": API_AXIS["focus"]},
    )
    assert "VERDICT: clean" in axis_prompt
    assert "Critical" in axis_prompt and "Nice-to-have" in axis_prompt
    assert [a["title"] for a in AXES] == [
        "Correctness",
        "Readability and simplicity",
        "Architecture",
        "Security",
        "Performance",
    ]
