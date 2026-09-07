"""함께 보면 좋은 지난 글 — 발행 기록에 쌓인 주소 가운데 오늘과 가까운 주제를 고른다.

내부 링크는 유입 자체를 만들지는 않지만, 들어온 사람이 한 편 더 보게 만든다.
주소가 없는 날(발행 기록을 안 적은 날)은 링크할 수 없으므로 건너뛴다.
"""

from __future__ import annotations

import json
from pathlib import Path

from .cluster import similarity
from .config import Config
from .store import PublishLog, TitleLog


def _day_payload(output_dir: Path, day: str) -> dict:
    path = output_dir / day / "data.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _topics(payload: dict) -> list[str]:
    out = [payload.get("headline", "")]
    out += [i.get("title", "") for i in payload.get("issues", []) or []]
    return [t for t in out if t]


def today_topics(brief, output_dir: Path, run_date: str) -> list[str]:
    if brief is not None:
        return [brief.headline] + [i.title for i in brief.issues]
    return _topics(_day_payload(output_dir, run_date))


def related_posts(cfg: Config, run_date: str, brief=None, limit: int = 3) -> list[dict]:
    """오늘 주제와 가까운 순으로 지난 발행 글 최대 limit 개. 주제가 안 겹치면 최근 순."""
    published = PublishLog(cfg.state_dir / "published.json")
    if not published.days:
        return []
    titles = TitleLog(cfg.state_dir / "titles.json")
    mine = today_topics(brief, cfg.output_dir, run_date)

    rows: list[dict] = []
    for day, entry in published.days.items():
        url = (entry.get("url") or "").strip()
        if day == run_date or not url.startswith(("http://", "https://")):
            continue
        payload = _day_payload(cfg.output_dir, day)
        blog = (titles.days.get(day, {}) or {}).get("blog", {}) or {}
        title = blog.get("title") or payload.get("headline") or f"{day} 브리핑"
        score = max((similarity(a, b) for a in mine for b in _topics(payload)), default=0.0)
        rows.append({"date": day, "title": title, "url": url, "score": round(score, 3)})

    rows.sort(key=lambda r: (-r["score"], r["date"]), reverse=False)
    rows.sort(key=lambda r: (-r["score"], -_as_number(r["date"])))
    return rows[:limit]


def _as_number(day: str) -> int:
    return int(day.replace("-", "") or 0)
