"""config/*.yaml 로딩 + 점 표기 접근."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


@dataclass
class Feed:
    id: str
    name: str
    url: str
    weight: float = 1.0
    enabled: bool = True


@dataclass
class Config:
    settings: dict[str, Any]
    sources: dict[str, Any]
    config_dir: Path
    repo_root: Path = REPO_ROOT

    # ── 편의 접근자 ──────────────────────────────────────────

    def get(self, path: str, default: Any = None) -> Any:
        """'collect.timeout_seconds' 처럼 점으로 중첩 값을 읽는다."""
        node: Any = self.settings
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def feeds(self) -> list[Feed]:
        out: list[Feed] = []
        for raw in self.sources.get("feeds", []) or []:
            feed = Feed(
                id=raw["id"],
                name=raw.get("name", raw["id"]),
                url=raw["url"],
                weight=float(raw.get("weight", 1.0)),
                enabled=bool(raw.get("enabled", True)),
            )
            out.append(feed)
        return out

    @property
    def enabled_feeds(self) -> list[Feed]:
        return [f for f in self.feeds if f.enabled]

    @property
    def keywords(self) -> dict[str, dict[str, Any]]:
        return self.sources.get("keywords", {}) or {}

    @property
    def exclude_terms(self) -> list[str]:
        return self.sources.get("exclude_terms", []) or []

    @property
    def exclude_patterns(self) -> list[str]:
        return self.sources.get("exclude_patterns", []) or []

    @property
    def require_terms(self) -> list[str]:
        return self.sources.get("require_terms", []) or []

    @property
    def breaking_tags(self) -> list[str]:
        return self.sources.get("breaking_tags", []) or []

    @property
    def output_dir(self) -> Path:
        raw = self.get("output.dir", "output")
        path = Path(raw)
        return path if path.is_absolute() else self.repo_root / path

    @property
    def state_dir(self) -> Path:
        return self.repo_root / "state"

    @property
    def api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or None

    @property
    def paste_mode(self) -> bool:
        """`llm.mode: paste` — API 를 부르지 않고 사람이 claude.ai 에 붙여넣는다 (0원)."""
        return str(self.get("llm.mode", "api") or "api").strip().lower() == "paste"

    @property
    def llm_transport(self) -> str:
        """`subscription` — 구독 토큰으로 Claude Code 를 부른다(API 요금 0). `api` — API 키로 부른다(요금)."""
        return str(self.get("llm.transport", "api") or "api").strip().lower()

    @property
    def llm_ready(self) -> bool:
        """모델을 부를 인증이 있는가. **구독으로 정해 두었으면 API 키가 있어도 쓰지 않는다** —
        토큰이 없다고 조용히 API 로 넘어가면 사용자가 끊으려던 요금이 다시 나간다."""
        if self.llm_transport == "subscription":
            from .subscription import ready
            return not ready(self)
        return bool(self.api_key)

    @property
    def llm_enabled(self) -> bool:
        """설정에서 켜져 있고 인증(구독 토큰 또는 API 키)도 있어야 실제로 호출한다. 붙여넣기 모드면 언제나 아니다."""
        return (bool(self.get("llm.enabled", True)) and self.llm_ready
                and not self.paste_mode)


def load_config(config_dir: str | Path | None = None) -> Config:
    directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    settings = _read_yaml(directory / "settings.yaml")
    sources = _read_yaml(directory / "sources.yaml")
    _load_dotenv(REPO_ROOT / ".env")
    return Config(settings=settings, sources=sources, config_dir=directory)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"설정 파일이 없습니다: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_dotenv(path: Path) -> None:
    """.env 를 읽어 환경변수로 올린다. 이미 설정된 값은 덮지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
