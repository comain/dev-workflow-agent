"""Jinja pages and escaped markdown."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mistune
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Documents are GFM in practice -- the prompts ask for tables, checklists and
#: coverage matrices -- and mistune parses none of that without plugins: a table
#: renders as a paragraph of pipes. `escape=True` stays, so an artifact cannot
#: inject HTML no matter which plugin parsed it.
class _Renderer(mistune.HTMLRenderer):
    """Standard HTML, except that a mermaid fence becomes a diagram.

    Mermaid reads the element's text content, so the source stays escaped here
    and the browser decodes it back -- a diagram cannot smuggle markup in. When
    the library does not load, the block renders as its own source, which is
    poor but readable; a missing diagram should not be a blank space.
    """

    def block_code(self, code, info=None):
        if (info or "").strip().split(None, 1)[:1] == ["mermaid"]:
            return f'<pre class="mermaid">{mistune.util.escape(code)}</pre>\n'
        return super().block_code(code, info)


_md = mistune.create_markdown(
    renderer=_Renderer(escape=True),
    plugins=[
        "table",
        "strikethrough",
        "task_lists",
        "footnotes",
        "def_list",
        "url",
    ],
)
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def markdown_html(text: str) -> str:
    return _md(text or "")


def render_template(name: str, **values: Any) -> str:
    return _env.get_template(name).render(**values)


def artifact_or_empty(store, task_id: str, name: str) -> str:
    from dev_flow_agent.artifacts import UnknownArtifact, read_artifact

    try:
        return read_artifact(store, task_id, name).decode("utf-8")
    except (FileNotFoundError, UnknownArtifact, OSError):
        return ""
