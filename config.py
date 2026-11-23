"""Explicit .env configuration, with process environment taking precedence."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def data_dir() -> Path:
    return (
        Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "anki-vocabulary"
    )


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    db_path: Path = Field(default_factory=lambda: data_dir() / "queue.sqlite3")
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:3b"
    ollama_binary: str = "ollama"
    ollama_start_timeout: float = Field(default=30, gt=0, le=300)
    generation_timeout: float = Field(default=300, gt=0)
    batch_size: int = Field(default=8, ge=1, le=50)
    anki_url: str = "http://127.0.0.1:8765"
    anki_api_key: str | None = None
    anki_deck: str = "English Vocabulary"
    anki_note_type: str = "Basic"
    anki_front_field: str = "Front"
    anki_back_field: str = "Back"
    anki_tags: list[str] = Field(default_factory=lambda: ["english", "vocabulary"])
    anki_sync: bool = False
    anki_profile: str = ""  # Set this when using multiple profiles.
    anki_wait_timeout: float = Field(default=30, ge=0, le=300)
    max_attempts: int = Field(default=3, ge=1, le=100)
    process_when_anki_open: bool = True
    wordnet: bool = False

    @field_validator("db_path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("ollama_url", "anki_url")
    @classmethod
    def local_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("Use an http:// loopback URL without credentials or path")
        _ = url.port  # Validate the port.
        return value.rstrip("/")

    @field_validator("anki_tags")
    @classmethod
    def valid_tags(cls, value: list[str]) -> list[str]:
        if any(not tag or any(c.isspace() for c in tag) for tag in value):
            raise ValueError("Tags must be nonempty and contain no whitespace")
        return value

    @field_validator("ollama_model")
    @classmethod
    def local_model(cls, value: str) -> str:
        if not value.strip() or "cloud" in value.lower() or "/" in value or "://" in value:
            raise ValueError("Use a locally installed model name, not a cloud model")
        return value

    @model_validator(mode="after")
    def distinct_fields(self) -> "Config":
        if self.anki_front_field == self.anki_back_field:
            raise ValueError("Front and back field mappings must differ")
        return self


def load_config(path: str | Path | None = None) -> Config:
    selected = path or os.environ.get("VOCAB_CONFIG")
    config_path = (
        Path(selected).expanduser()
        if selected
        else (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "anki-vocabulary/config.env"
        )
    )
    if selected and not config_path.is_file():
        raise ValueError(f"Configuration file does not exist: {config_path}")
    values = dotenv_values(config_path, interpolate=False) if config_path.is_file() else {}
    options: dict[str, object] = {}
    for name in Config.model_fields:
        key = f"VOCAB_{name.upper()}"
        value = os.environ.get(key, values.get(key))
        if value is not None:
            options[name] = value.split() if name == "anki_tags" else value
    return Config.model_validate(options)
