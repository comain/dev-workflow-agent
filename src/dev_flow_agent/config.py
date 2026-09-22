"""Product settings. Env prefix DFA_."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DFA_", extra="ignore")

    data_dir: Path = Field(default=Path("./var"))
    allow_anonymous_gates: bool = False
    #: Optional Maven Central mirror. Set it when the default Central endpoint
    #: is not reachable from the build environment.
    maven_central_mirror_url: str = ""
    #: Optional URL of the test-enforcement usage guide. The turn fetches it
    #: rather than working from a copy: commands change, and a prompt that
    #: pins them is wrong the day after it is written.
    enforcement_guide: str = ""
    root_path: str = ""

    @field_validator("root_path")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        text = (value or "").strip()
        if not text or text == "/":
            return ""
        if not text.startswith("/"):
            text = "/" + text
        return text.rstrip("/")

    @property
    def tasks_db(self) -> Path:
        return self.data_dir / "tasks.db"

    @property
    def runtime_db(self) -> Path:
        return self.data_dir / "runtime.db"

    @property
    def artifacts_root(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def checkpoints_root(self) -> Path:
        return self.data_dir / "checkpoints"

    @property
    def workspaces_root(self) -> Path:
        return self.data_dir / "workspaces"
