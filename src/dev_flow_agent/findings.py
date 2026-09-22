"""What a review actually found, as opposed to what it declared.

Two things went wrong on the same review, and they are the same mistake in
different places: the verdict was not a consequence of the findings.

One axis reported a single **Important** finding and then wrote
`VERDICT: critical`, which sent the run round the repair loop until it ran out
of rounds and stopped for a human. And the finding itself was about a null
check in code the branch never touched -- a fair observation about the file,
not about the change under review, and not something the run could fix by
building.

So the verdict is derived here instead: a review gates when it reports a
Critical or Important finding *on a file this branch changed*. Nice-to-have
never gates, and neither does anything under a `## Pre-existing` heading --
both are still written down and still read by a person.

Important was left out at first, on the grounds that only Critical must not
ship. Then a review reported five Important findings about the run's own new
code -- among them a silent partial update on a retry path whose design
document asserted the opposite -- and the run finished anyway. "Must not
ship" is the wrong test for work still being written: a defect in what this
run just wrote is work it has left to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, List, Optional, Sequence, Tuple

#: `- **Critical** `File.java:174` dereferences ...` -- and the shape the
#: reviewers actually write, which puts the severity and the headline inside
#: one bold run: `**Important - silent partial update ...**`. Requiring the
#: closing `**` right after the word found nothing in a review full of
#: findings, and a review with no findings reads as clean.
SEVERITY = re.compile(
    r"(?:\*\*\s*(critical|important|nice-to-have)\b"
    r"|severity:\s*(critical|important|nice-to-have)\b"
    r"|^\s*[-*]\s*(critical|important|nice-to-have)\b)",
    re.IGNORECASE | re.MULTILINE,
)

#: A heading that says the findings under it are not this run's to fix.
PRE_EXISTING = re.compile(r"^#{1,6}\s*pre-existing\b", re.IGNORECASE | re.MULTILINE)

#: What stops a run. `nice-to-have` never does; a `pre-existing` section never
#: does, whatever it is marked.
GATING_SEVERITIES = ("critical", "important")

#: A path with a line number: `provider/src/main/java/...OrderTask.java:174`.
LOCATION = re.compile(r"([\w./\\-]+\.[A-Za-z0-9]+):(\d+)")


@dataclass(frozen=True)
class Finding:
    severity: str
    paths: Tuple[str, ...]
    text: str
    #: True when the finding sits under a `## Pre-existing` heading, where a
    #: reviewer puts what the branch did not cause.
    pre_existing: bool = False

    def touches(self, changed: Sequence[str]) -> bool:
        """Whether this finding cites a file the branch changed.

        A finding with no location at all is kept: "the change has no tests"
        names no file and is still about the change.
        """
        if not self.paths:
            return True
        if not changed:
            return True
        names = {PurePosixPath(path).name for path in changed}
        for cited in self.paths:
            name = PurePosixPath(cited.replace("\\", "/")).name
            if name in names or any(path.endswith(cited) for path in changed):
                return True
        return False


def read_findings(review: str) -> List[Finding]:
    """Split a review into its findings, each with the severity it carries."""
    text = review or ""
    marks = list(SEVERITY.finditer(text))
    sections = [m.start() for m in PRE_EXISTING.finditer(text)]
    found: List[Finding] = []
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[match.start() : end]
        severity = next(group for group in match.groups() if group).lower()
        paths = tuple(path for path, _line in LOCATION.findall(body))
        # Under a `## Pre-existing` heading until the next heading of that
        # level; close enough to ask whether one started before this finding
        # and no ordinary heading came between.
        started = [start for start in sections if start < match.start()]
        between = text.rfind("\n## ", started[-1] if started else 0, match.start())
        under = bool(started) and between <= (started[-1] if started else 0)
        found.append(
            Finding(severity=severity, paths=paths, text=body.strip(), pre_existing=under)
        )
    return found


def gating(review: str, changed: Sequence[str]) -> List[Finding]:
    """The findings a run has to deal with before it is finished.

    Critical and Important, on files this run changed. Important was left out
    at first, on the grounds that only Critical must not ship -- and then a
    review reported five Important findings about the run's own new code,
    among them a silent partial update on a retry path whose design document
    asserted the opposite, and the run finished anyway.

    "Must not ship" is the wrong test for work that is still being written.
    A defect the reviewer found in what this run just wrote is work this run
    has left to do; the rounds bound how long it may take, and a person is
    asked when it runs out. Nice-to-have never gates, and neither does
    anything under a `## Pre-existing` heading.
    """
    return [
        finding
        for finding in read_findings(review)
        if finding.severity in GATING_SEVERITIES
        and not finding.pre_existing
        and finding.touches(changed)
    ]


def verdict(review: str, changed: Sequence[str]) -> str:
    """`critical` when something gates, `clean` otherwise."""
    return "critical" if gating(review, changed) else "clean"


def disagreement(declared: Optional[str], review: str, changed: Sequence[str]) -> str:
    """One line for the log when the declared verdict is not what was found."""
    found = verdict(review, changed)
    if declared is None or declared == found:
        return ""
    severities = [finding.severity for finding in read_findings(review)]
    if not severities:
        listed = "no findings"
    else:
        listed = ", ".join(sorted(set(severities)))
    outside = [
        finding
        for finding in read_findings(review)
        if finding.severity == "critical" and not finding.touches(changed)
    ]
    if outside:
        listed += f"; {len(outside)} Critical outside the change"
    return f"review said {declared}; its findings say {found} ({listed})"
