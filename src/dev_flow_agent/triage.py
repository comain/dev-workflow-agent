"""What kind of work this is, and therefore which path it takes.

Every task used to walk the same eight stages. A one-line null check went
through design, a critique of that design, and a plan whose single task was the
fix -- four model turns and two human gates spent describing work smaller than
the description. Triage asks what the work *is* first, and a fix takes the short
path.

The classification is proposed by an agent and confirmed by a person, because
it decides how much scrutiny the change receives and that is not a judgement to
make silently.
"""

from __future__ import annotations

from typing import Optional

from dev_flow_agent.declarations import declaration

#: A defect in existing behaviour, reported by a person or a monitor.
BUG_FIX = "bug-fix"
#: Addressing findings a reviewer already raised on existing work.
CR_FIX = "cr-fix"
#: New capability, or a change large enough to need designing.
FEATURE_DEV = "feature-dev"

KINDS = (BUG_FIX, CR_FIX, FEATURE_DEV)

#: The kinds that skip design and planning. Both start from a description of
#: what is wrong that is already concrete: a defect, or a reviewer's finding.
LEAN_KINDS = frozenset({BUG_FIX, CR_FIX})

def parse_kind(text: str) -> Optional[str]:
    """The kind the triage document declares, or None if it declares none."""
    return declaration(text, "KIND", KINDS)


def is_lean(kind: str) -> bool:
    return str(kind or "").lower() in LEAN_KINDS
