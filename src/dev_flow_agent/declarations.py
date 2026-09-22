"""Reading the one line a document declares its own result on.

Three documents end with a machine-readable verdict -- the triage's kind, the
enforcement gate's pass or fail, a reviewer's clean or critical -- because
reading a result out of prose does not survive contact with prose: matching the
word "critical" once classified "No Critical findings" as a Critical finding.

The rule they share is that the *last* declaration wins. A document that quotes
its own instructions, which they are asked for explicitly and so often do, has
the example above its answer; taking the first match reads the example.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional


def declaration(text: str, name: str, options: Iterable[str]) -> Optional[str]:
    """The value declared on a `NAME: value` line, or None if none is."""
    allowed = "|".join(re.escape(option) for option in options)
    pattern = re.compile(
        rf"^\s*{re.escape(name)}:\s*({allowed})\b", re.IGNORECASE | re.MULTILINE
    )
    found = pattern.findall(text or "")
    return found[-1].lower() if found else None
