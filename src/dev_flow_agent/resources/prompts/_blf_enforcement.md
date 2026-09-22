{% if jira %}
## Issue-tracker test enforcement (mandatory)

The ordinary test suite is necessary but not sufficient. Missing, skipped or
failed enforcement evidence is a blocker, not a warning — record the command
and its result.

Find the command in this order, and stop at the first one that answers:

- `doc/test-enforce-usage.md`, if the repo has it. Some do, and it is then the
  authority — it records the module list, base ref and test selection someone
  already got right. Many repos do not have it. Its absence is not a finding.
- The POM's `test-enforcement` profile, which is the gate itself. It is
  activated by `-Dtest.enforcement.enabled=true`, and its properties name the
  base ref, the diff-coverage floor and the mutation threshold to meet.

`dev_gate.py`, where the environment has it, makes the result
deterministic:
`dev_gate.py pre-review --jira {{ jira }} --test-enforcement-cmd '<repo command>'`.
It is usually absent here. Then run the command yourself and record it, its
exit status and the evidence it printed. Do not report a gate you could not run
as passed.

**Java projects**

{% if maven_settings is defined and maven_settings %}- **Pass `-gs {{ maven_settings }}` to every `mvn` command.** Maven Central is
  not reachable from this machine. That file mirrors `central`. Without it the
  build dies in TLS (`PKIX path building failed`) on the first dependency that
  has to be fetched, which looks like a broken repository and is not one.
{% endif %}- `[WARNING] The POM for <artifact> is missing, no dependency information
  available` is a warning. Maven says it and carries on, the build compiles and
  the tests run. It is not a blocker and not a finding -- report what the gate
  itself said.
- Verify the plugin is actually active before trusting a result:
  `mvn -Dtest.enforcement.enabled=true help:effective-pom` must show
  `test-enforcer` under active `build/plugins`. A parent POM version or a
  `pluginManagement` declaration does not prove the gate is installed.
- Put tests in the module that owns the changed production code, or in one that
  definitely produces JaCoCo XML for the changed lines.
- Use fully qualified class names in `-DtargetTests`. A short name can let
  Surefire run tests while PIT reports "No test classes identified to scan".
- For multi-module changes, run from the repo root with the target test classes
  of every changed module.
- **In a Java 8 repo, run both the ordinary tests and the enforcement commands
  with the Java 8 `JAVA_HOME`.**
- Treat Alibaba Java Coding Guidelines *Mandatory* violations in changed code as
  blockers, and keep style-only cleanup out of a feature change.

**Tests are off until the command turns them on**

`ripple-root` and its relatives set `<skipTests>true</skipTests>`, so a Maven
run skips every test unless told otherwise -- and it still prints
`BUILD SUCCESS`, still reports diff coverage from whatever `jacoco.exec` was
left in `target/`, and reads exactly like a pass. It is not one: nothing ran.

Pass the flags on the command line, where the real usage documents put them:

    -DskipTests=false -Dmaven.test.skip=false

Never turn tests on by editing the POM. The evidence must contain a
`Tests run: N, Failures: 0, Errors: 0` line with N above zero, and a PIT
statistics block; without both, the gate has not run, whatever Maven's exit
status says.

**The gate is read-only**

You may add tests and change production code. You may not change what the gate
measures you by: the `test-enforcement` profile, `test-enforcer`, PIT, JaCoCo
or Surefire configuration, the coverage floor, the mutation threshold,
`targetClasses`, `targetTests`, or any skip flag. In particular `targetClasses`
is `${targetClasses}` because `filter-diff` computes it from the changed code
-- replacing it with a class name you chose is narrowing the gate to the test
you already pass. A run that edits any of this stops for a person, whatever
the gate then says.

**Reading a multi-module reactor**

`no changed Java source lines for <module>` is the normal, correct output for
every module your change does not touch. It is not a failure and it is not a
finding. The verdict comes from the module that owns the changed code, whose
line reads `diff line coverage NN% passed|failed for <module> (n/m)`. PIT
likewise reports per module: `Created 0 mutation test units` in a module with
no changed code says nothing about your change.

So quote the lines you actually judged by, naming the module. A verdict that
generalises another module's message across the reactor is wrong in both
directions -- and the direction that reports a clean gate as blocked is the
one that wastes the rounds.

**When the gate is not installed, install it**

A project with no `test-enforcement` profile has not opted out of enforcement;
it has not been onboarded yet. That is a thing to fix, not a reason to report
`blocked`.

Read the usage guide before wiring anything:

    {{ enforcement_guide if enforcement_guide is defined and enforcement_guide else 'the dev-skills reference test-enforce-usage.md' }}

It is the authority and it moves: which parent carries the profile, which
versions are new enough, what a project that inherits neither should do, and
the exact commands. Fetch it and follow the section on enabling a project that
does not have enforcement yet -- do not work from memory of these rules, and
do not copy a version number out of this prompt, because there is none here on
purpose.

Then run the gate and report its evidence as usual. Say in the document that
the project was not enforced before, which section of the guide you followed,
and what you changed to enforce it, so the reviewer sees an onboarding as well
as a result.

This is the one POM change the gate welcomes. Narrowing an existing profile --
its thresholds, its selections, its skip flags -- is still refused, and a run
that does it stops for a person.

**You answer for your own tests, not for the repository's**

A repository whose default is `skipTests=true` has tests nobody has run in a
long time, and some of them fail. They are not this change's doing and not
this change's to fix: a run that tries will spend its rounds on somebody
else's problem, and a run that gets impatient will delete them, which is how
one run produced a green gate over an unwritten fix.

So scope the run to the tests that cover what you changed, the way the real
usage documents do:

    -Dtest=<your test classes> -DtargetTests=<their fully qualified names>
    -Dsurefire.failIfNoSpecifiedTests=false -DfailIfNoTests=false

Selecting your own tests on the command line is not narrowing the gate -- the
changed-line coverage and mutation thresholds still apply to your code, which
is what the gate measures. Editing the POM to exclude classes *is* narrowing
it, and is refused.

When a test outside your scope fails, prove it is pre-existing before setting
it aside: run that same test at the base ref and show it failing there too.
Then record it in the document as something a person should know about -- name
the class, the error, and that it fails on `origin/master` -- and carry on.
Never delete, `@Ignore`, or comment out a test to make a gate pass; a run that
removes tests it did not write stops for a person, whatever the gate says.

**Mutation results are a survivor gate, not a coverage gate**

- `NO_COVERAGE` mutants are noise for test strength: they mark uncovered code
  without reducing strength.
- `SURVIVED` mutants on changed lines, or on meaningful covered behaviour, are
  real gaps and are blockers unless you can show the mutation is equivalent.
- Do not make the gate green by excluding classes. Read `target/pit-reports`,
  classify the mutants, and strengthen assertions on externally visible
  behaviour — return values, state changes, emitted events, collaborator calls.
  Document any exclusion you do make and why it is not a behaviour gap.
- A green gate proves nothing until you know what it gated: after any POM,
  profile, `targetClasses` or `excludedMethods` change, read the PIT
  target/classes summary and confirm the changed production code was selected.

**Python projects** need `uta python-enforce`, or the lightweight enforcement
tool with `UTA_PYTHON_ENFORCE_SCRIPT` pointing at it. Plain `pytest` or
`coverage.py` output without a `UTA_PYTHON_ENFORCEMENT_EVIDENCE=...` marker is
missing evidence. Record the Python version, the dependency setup, the
pytest/coverage/mutmut output and the base ref.
{% endif %}
