{% if jira %}
## Issue-tracker requirements for {{ jira }}

This is issue-tracked feature work, so the issue-tracker overlay applies. It is not a separate
process — fold it into what you are already doing.

**The document set lives under `doc/`, keyed by the issue.**

| Artifact | Path |
| --- | --- |
| Spec | `doc/spec-{{ jira }}.md` |
| Design (overview) | `doc/design-{{ jira }}.md` |
| Design (per-repo detail) | `doc/design-{{ jira }}-<repo>.md`, folded into the overview for single-repo work |
| Usage | `doc/usage-{{ jira }}.md` |
| Release approval evidence | `doc/release-approval-{{ jira }}.evidence.json` |

A document that does not apply is still created, containing `N/A` and the
reason — review and ship gates need a stable place to check documentation
status, and an absent file cannot be told from a forgotten one. Never use a
root-level `SPEC.md` for issue-tracked work.

Record the Jira key, the original issue context, and the status of the design
and usage documents in `doc/spec-{{ jira }}.md`. When requirements change,
update the spec and its linked documents before implementation continues.

**Branch and version.** The branch is named `$jira-$date` and based on
`origin/master` unless a different base was given. For API repos the
development and test version is `{{ jira }}-SNAPSHOT`; a snapshot version is
never shipped, because CI bumps the release version before deployment.

**Release approval is a ship-time gate, not a review one.** Production-bound
work eventually needs Release approval for the release-approval flow, evidence at
`doc/release-approval-{{ jira }}.evidence.json`, and RDC
`data.rdcDeployAllowed=true` before deployment. None of that is required for
the work you are doing now, and its absence is not a defect in this change --
do not chase it, and do not report it missing.
{% else %}
## Non-Jira tooling work

This run carries no Jira key, so do not add issue-tracker documents to it.
Record that decision in the spec, use stable paths such as `docs/spec-<topic>.md`
and `docs/design-<topic>.md`, and still do the same source exploration, design
review, verification planning, rollout planning and open-question tracking.
{% endif %}
