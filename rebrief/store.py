"""수집 이력 저장 — 어제 다룬 기사를 오늘 또 다루지 않기 위한 장치."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Article

SEEN_FILE = "seen.json"


class SeenStore:
    """기사 ID → 처음 다룬 날짜."""

    def __init__(self, path: Path):
        self.path = path
        self.seen: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.seen = data.get("articles", {}) or {}
        except (json.JSONDecodeError, OSError):
            # 이력이 깨졌다고 파이프라인을 세울 이유는 없다. 비우고 다시 쌓는다.
            self.seen = {}

    def filter_new(self, articles: list[Article], skip_days: int) -> list[Article]:
        """최근 skip_days 안에 이미 다룬 기사를 걸러낸다."""
        if skip_days <= 0:
            return articles
        cutoff = date.today() - timedelta(days=skip_days)
        fresh = []
        for article in articles:
            stamp = self.seen.get(article.id)
            if stamp and _parse_date(stamp) and _parse_date(stamp) > cutoff:
                continue
            fresh.append(article)
        return fresh

    def mark(self, articles: list[Article], run_date: date) -> None:
        stamp = run_date.isoformat()
        for article in articles:
            self.seen.setdefault(article.id, stamp)

    def prune(self, keep_days: int = 30) -> None:
        cutoff = date.today() - timedelta(days=keep_days)
        self.seen = {
            key: value for key, value in self.seen.items()
            if (parsed := _parse_date(value)) is None or parsed > cutoff
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(self.seen),
            "articles": dict(sorted(self.seen.items())),
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def save_raw(path: Path, articles: list[Article], meta: dict) -> None:
    """수집 원본을 남겨 둔다. render 명령으로 언제든 재생성할 수 있다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "articles": [json.loads(a.model_dump_json()) for a in articles],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_raw(path: Path) -> tuple[list[Article], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    articles = [Article.model_validate(item) for item in data.get("articles", [])]
    return articles, data.get("meta", {})
