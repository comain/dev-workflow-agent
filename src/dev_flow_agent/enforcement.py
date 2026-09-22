"""Read the enforcement gate's own output, rather than its reader's summary.

Twice in one run the gate's verdict came from prose about the log instead of
the log. A turn read the four modules that correctly said `no changed Java
source lines` -- because they held no changed code -- generalised them across
the reactor, and reported a gate that had just passed at 100% as blocked. It
did that three rounds running, each one a full Maven build and each one ending
in a human gate.

The lines that carry the verdict are printed by the tools, in fixed shapes:

    [test-enforcer] diff line coverage 100.00% passed for <module> (11/11)
    >> Generated 28 mutations Killed 28 (100%)
    >> Mutations with no coverage 0. Test strength 100%
    Tests run: 26, Failures: 0, Errors: 0, Skipped: 0
    BUILD SUCCESS

So read those. The turn still writes the document -- what was run, what it
means, what to do about it -- but what the gate *found* is not a matter of
opinion, and the pipeline no longer takes an opinion about it.

The evidence is only as honest as the document quoting it, which is the same
trust already placed in the whole report. What this removes is not dishonesty
but misreading, which is what actually happened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: `[test-enforcer] diff line coverage 100.00% passed for <module> (11/11)`
COVERAGE = re.compile(
    r"diff line coverage\s+([\d.]+)%\s+(passed|failed)\s+for\s+(\S+)\s*\((\d+)\s*/\s*(\d+)\)",
    re.IGNORECASE,
)

#: `>> Generated 28 mutations Killed 28 (100%)`
MUTATIONS = re.compile(r"Generated\s+(\d+)\s+mutations?\s+Killed\s+(\d+)", re.IGNORECASE)

#: `>> Mutations with no coverage 0. Test strength 100%`
STRENGTH = re.compile(r"Test strength\s+([\d.]+)%", re.IGNORECASE)

#: `>> Mutations with no coverage 17.` -- mutants on lines no test reaches.
NO_COVERAGE = re.compile(r"Mutations with no coverage\s+(\d+)", re.IGNORECASE)

#: `Tests run: 26, Failures: 0, Errors: 0, Skipped: 0`
TESTS = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)", re.IGNORECASE
)

#: `[test-enforcer] ... pitest.targets=1 [com.example.Thing*]`
TARGETS = re.compile(r"pitest\.targets=(\d+)", re.IGNORECASE)

FAILURE = re.compile(r"\bBUILD FAILURE\b")
SUCCESS = re.compile(r"\bBUILD SUCCESS\b")


@dataclass(frozen=True)
class Evidence:
    """What the gate's own output says, with nothing inferred."""

    #: (module, percentage, passed) per module that had changed lines.
    coverage: Tuple[Tuple[str, float, bool], ...] = ()
    #: (generated, killed) per mutation run that produced statistics.
    mutations: Tuple[Tuple[int, int], ...] = ()
    strengths: Tuple[float, ...] = ()
    #: Mutants on lines no test reaches, per run that reported them.
    uncovered: Tuple[int, ...] = ()
    #: (run, failures, errors) per reported test execution.
    tests: Tuple[Tuple[int, int, int], ...] = ()
    targets: Tuple[int, ...] = ()
    build_failed: bool = False
    build_succeeded: bool = False
    reasons: Tuple[str, ...] = field(default=())

    @property
    def measured(self) -> bool:
        """Whether the gate measured this change at all.

        A module reporting `no changed Java source lines` is not evidence of
        anything -- every module outside the change says it, correctly.
        """
        return bool(self.coverage) or any(n > 0 for n in self.targets)

    @property
    def from_maven(self) -> bool:
        """Whether this report is quoting a Maven gate at all.

        A Python project's enforcement prints none of these lines, and its
        verdict is the turn's to declare. Only a report that shows the Java
        gate's own output is held to the Java gate's own output.
        """
        return bool(
            self.coverage
            or self.targets
            or self.tests
            or self.mutations
            or self.build_failed
            or self.build_succeeded
        )

    @property
    def ran_tests(self) -> bool:
        """Whether any test actually executed.

        `ripple-root` sets `<skipTests>true</skipTests>`, so the whole family
        skips tests unless the command says otherwise -- and a run that skips
        them still reports `BUILD SUCCESS`, still reports diff coverage from
        whatever `jacoco.exec` was lying in `target/`, and looks exactly like a
        pass. The command has to carry `-DskipTests=false
        -Dmaven.test.skip=false`; the evidence has to show it did.
        """
        return any(run > 0 for run, _, _ in self.tests)

    @property
    def mutated(self) -> bool:
        """Whether a mutation run produced statistics on real mutants."""
        return any(generated > 0 for generated, _ in self.mutations)

    @property
    def nothing_to_mutate(self) -> bool:
        """Whether the gate itself reported no changed production code.

        `pitest.targets=0` from a healthy gate is a no-op, not a gap: having
        nothing to measure is a different thing from measuring nothing.
        """
        return bool(self.targets) and all(n == 0 for n in self.targets)

    def survivors(self) -> int:
        """Mutants that a test reached and failed to kill."""
        total = 0
        for index, (generated, killed) in enumerate(self.mutations):
            uncovered = self.uncovered[index] if index < len(self.uncovered) else 0
            total += max(generated - killed - uncovered, 0)
        return total

    def verdict(self) -> Optional[str]:
        """`pass`, `fail`, or None when the output does not settle it.

        `pass` is the narrow one: everything the gate is supposed to establish
        has to be in the output. Anything less is None -- unsettled, for the
        turn to explain -- rather than a pass nobody checked.
        """
        if self.build_failed:
            return "fail"
        if any(failures or errors for _, failures, errors in self.tests):
            return "fail"
        if any(not passed for _, _, passed in self.coverage):
            return "fail"
        # Survivors, which is what mutation testing gates on. A mutant nothing
        # covers did not survive a test -- no test ran against it, and PIT
        # mutates the whole targeted class, so most of those sit on
        # pre-existing lines this change never touched. Counting them as
        # failures failed a gate that had killed every mutant it reached.
        if self.survivors():
            return "fail"
        if not (self.measured and self.build_succeeded and self.ran_tests):
            return None
        # Mutation evidence is required unless the gate says there was nothing
        # to mutate, which it is entitled to say.
        if not (self.mutated or self.nothing_to_mutate):
            return None
        return "pass"


def read_evidence(text: str) -> Evidence:
    """Pull the tools' own lines out of an enforcement report."""
    body = text or ""
    coverage = tuple(
        (module, float(percent), verdict.lower() == "passed")
        for percent, verdict, module, _covered, _total in COVERAGE.findall(body)
    )
    mutations = tuple(
        (int(generated), int(killed)) for generated, killed in MUTATIONS.findall(body)
    )
    strengths = tuple(float(value) for value in STRENGTH.findall(body))
    uncovered = tuple(int(value) for value in NO_COVERAGE.findall(body))
    tests = tuple(
        (int(run), int(failures), int(errors))
        for run, failures, errors in TESTS.findall(body)
    )
    targets = tuple(int(value) for value in TARGETS.findall(body))
    return Evidence(
        coverage=coverage,
        mutations=mutations,
        strengths=strengths,
        uncovered=uncovered,
        tests=tests,
        targets=targets,
        build_failed=bool(FAILURE.search(body)),
        build_succeeded=bool(SUCCESS.search(body)),
    )


def disagreement(declared: Optional[str], evidence: Evidence) -> str:
    """One line for the event log when the reader and the gate disagree."""
    found = evidence.verdict()
    if found is None or declared is None or found == declared:
        return ""
    parts: List[str] = []
    for module, percent, passed in evidence.coverage:
        parts.append(f"{module} {percent:g}% {'passed' if passed else 'failed'}")
    for generated, killed in evidence.mutations:
        if generated:
            parts.append(f"{killed}/{generated} mutants killed")
    if evidence.survivors():
        parts.append(f"{evidence.survivors()} survived")
    return f"report said {declared}; its own output says {found} ({'; '.join(parts)})"
