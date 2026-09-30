"""Config loading. One YAML file; env vars only for secrets and overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RoleConfig:
    provider: str
    model: str
    max_tokens: int = 8000
    effort: str | None = None


@dataclass
class Config:
    roles: dict[str, RoleConfig]
    pricing: dict[str, dict[str, float]]
    limits: dict[str, Any]
    story: dict[str, Any]
    hitl: dict[str, Any]
    paths: dict[str, str]
    anthropic: dict[str, Any] = field(default_factory=dict)

    def role(self, name: str) -> RoleConfig:
        return self.roles[name]

    def path(self, key: str) -> Path:
        p = Path(self.paths[key])
        return p if p.is_absolute() else ROOT / p


def load_config(path: str | Path | None = None, provider_override: str | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path or os.environ.get("SERIAL_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text())
    provider_override = provider_override or os.environ.get("SERIAL_PROVIDER")
    roles = {}
    for name, r in raw["roles"].items():
        rc = RoleConfig(**r)
        if provider_override == "fake":
            rc = RoleConfig(provider="fake", model="fake", max_tokens=rc.max_tokens)
        roles[name] = rc
    return Config(
        roles=roles,
        pricing=raw["pricing"],
        limits=raw["limits"],
        story=raw["story"],
        hitl=raw["hitl"],
        paths=raw["paths"],
        anthropic=raw.get("anthropic", {}),
    )
