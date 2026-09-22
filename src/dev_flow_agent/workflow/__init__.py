"""Product workflow: YAML graph plus persist nodes."""

from pathlib import Path

FLOW_PATH = Path(__file__).resolve().parent / "flow.yaml"
PROMPT_DIR = Path(__file__).resolve().parent.parent / "resources" / "prompts"
