"""Point Maven at a Central mirror before a turn spends twenty minutes finding out.

Some builds cannot reach Maven Central, and a repository entry in a profile
never displaces `central`; only a `<mirror>` does. Write a settings file that
mirrors `central` and pass it as `-gs`. Turns compose their own Maven
commands, so they need the path, not a wrapper.

`next_task` asks for this on every pass rather than a node writing it once
after the checkout, for the reason `jira` and `base` are handled there: a run
resumed from a checkpoint never re-runs the nodes above it, so a key written
only at the start is missing for exactly the runs that loop.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

#: Where the file goes when there is nowhere better: inside the checkout. Kept
#: out of commits by `exclude_agent_noise` -- which it was not, at first, and a
#: repair turn committed the settings file to the branch it was building.
CACHE = Path(".dfa_cache") / "maven"

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <mirrors>
    <mirror>
      <id>dfa-central-mirror</id>
      <name>Mirror of Maven Central</name>
      <url>{url}</url>
      <mirrorOf>central</mirrorOf>
    </mirror>
  </mirrors>
</settings>
"""


def write_settings(repo_path: str, url: str, turn_dir: str = "") -> str:
    """Write the mirror settings and return its path.

    Outside the checkout when there is a turn directory to hold it: a file
    written into the repository is a file the build turn can commit, and one
    did -- `.dfa_cache/maven/central-mirror-settings.xml` reached the branch,
    and then the branch's own diff.

    An empty URL means no mirror is configured, and the turn is told nothing
    rather than being handed a `-gs` that points at a file mirroring nowhere.
    """
    if not url:
        return ""
    if turn_dir:
        directory = Path(turn_dir) / "maven"
    elif repo_path:
        directory = Path(repo_path) / CACHE
    else:
        return ""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "central-mirror-settings.xml"
    path.write_text(TEMPLATE.format(url=escape(url)), encoding="utf-8")
    return str(path)


def settings_for(state, context) -> str:
    """The mirror settings for this run's checkout, written and ready to pass."""
    from dev_flow_agent.config import Settings

    url = str(context.get("maven_central_mirror_url") or "") or (
        Settings().maven_central_mirror_url
    )
    return write_settings(
        str(state.get("repo_path") or ""), url, str(state.get("turn_dir") or "")
    )
