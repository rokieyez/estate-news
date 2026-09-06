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


# ── 비용 이력 ────────────────────────────────────────────────

COSTS_FILE = "costs.json"


class CostLog:
    """실행마다 쓴 토큰과 비용을 쌓는다. 문서의 '하루 약 N원' 을 실측으로 바꾸기 위한 장부."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[dict] = []
        if path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8")).get("entries", []) or []
            except (json.JSONDecodeError, OSError):
                self.entries = []

    def record(self, run_date: str, usage, *, kind: str = "daily") -> dict:
        """Usage 객체(rebrief.llm.Usage) 한 건을 기록한다. 호출이 없었으면 기록하지 않는다."""
        if not getattr(usage, "calls", 0):
            return {}
        entry = {
            "date": run_date,
            "kind": kind,
            "run_at": datetime.now().isoformat(timespec="seconds"),
            "model": usage.model,
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "usd": round(usage.estimated_usd, 4),
        }
        self.entries.append(entry)
        return entry

    def by_date(self) -> dict[str, float]:
        """날짜별 합계(USD). 같은 날 여러 번 돌렸으면 전부 더한다 — 실제로 쓴 돈이니까."""
        totals: dict[str, float] = {}
        for e in self.entries:
            totals[e["date"]] = totals.get(e["date"], 0.0) + float(e.get("usd", 0))
        return dict(sorted(totals.items()))

    def recent(self, days: int, *, today: date | None = None) -> tuple[float, int]:
        """최근 days 일 동안의 합계와 실행일 수."""
        today = today or date.today()
        cutoff = today - timedelta(days=days)
        hits = {d: v for d, v in self.by_date().items()
                if (parsed := _parse_date(d)) and cutoff < parsed <= today}
        return round(sum(hits.values()), 4), len(hits)

    def this_month(self, today: date | None = None) -> float:
        """이번 달(1일부터 오늘까지) 합계 USD."""
        today = today or date.today()
        prefix = today.strftime("%Y-%m")
        return round(sum(v for d, v in self.by_date().items() if d.startswith(prefix)), 4)

    def prune(self, keep_days: int = 400) -> None:
        cutoff = date.today() - timedelta(days=keep_days)
        self.entries = [e for e in self.entries
                        if (parsed := _parse_date(e.get("date", ""))) is None or parsed > cutoff]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(self.entries),
            "entries": self.entries,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── 수치 시계열 ──────────────────────────────────────────────

SERIES_FILE = "datapoints.json"


class SeriesStore:
    """매일 뽑힌 수치를 날짜별로 쌓는다.

    같은 지표(예: 서울 아파트 주간 변동률)가 여러 날 반복되면 그제야 '추이' 를 그릴 수
    있다. 없는 숫자를 지어내지 않고 추이 그래프를 얻는 유일한 길이 이 축적이다.
    """

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = []
        if path.exists():
            try:
                self.rows = json.loads(path.read_text(encoding="utf-8")).get("rows", []) or []
            except (json.JSONDecodeError, OSError):
                self.rows = []

    def record(self, run_date: str, datapoints: list[dict]) -> int:
        """같은 날짜의 기존 행은 갈아끼운다 (재실행 시 중복 방지). 추가된 행 수를 돌려준다."""
        self.rows = [r for r in self.rows if r.get("date") != run_date]
        added = 0
        for dp in datapoints:
            if not dp.get("label") or not dp.get("value"):
                continue
            self.rows.append({
                "date": run_date,
                "label": dp["label"],
                "value": str(dp["value"]),
                "unit": dp.get("unit", ""),
                "period": dp.get("period", ""),
                "source": dp.get("source", ""),
                "issue": dp.get("issue", ""),
            })
            added += 1
        self.rows.sort(key=lambda r: (r["date"], r["label"]))
        return added

    def prune(self, keep_days: int = 400) -> None:
        cutoff = date.today() - timedelta(days=keep_days)
        self.rows = [r for r in self.rows
                     if (parsed := _parse_date(r.get("date", ""))) is None or parsed > cutoff]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(self.rows),
            "rows": self.rows,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── 제목 기록장 ──────────────────────────────────────────────

TITLES_FILE = "titles.json"
TITLE_KINDS = ("blog", "longform", "shorts")


def title_type(title: str) -> str:
    """제목 유형을 거칠게 나눈다. 어떤 유형이 먹히는지 볼 때 쓴다."""
    t = title.strip()
    if "?" in t or t.endswith(("까", "까요", "일까", "나요")):
        return "질문형"
    if any(w in t for w in (" vs ", "VS", "보다", "비교")):
        return "비교형"
    if any(ch.isdigit() for ch in t):
        return "수치형"
    return "서술형"


class TitleLog:
    """날짜별 제목 후보와, 실제로 고른 것·조회수를 적는 장부."""

    def __init__(self, path: Path):
        self.path = path
        self.days: dict[str, dict] = {}
        if path.exists():
            try:
                self.days = json.loads(path.read_text(encoding="utf-8")).get("days", {}) or {}
            except (json.JSONDecodeError, OSError):
                self.days = {}

    def record_candidates(self, run_date: str, kind: str, candidates: list[str]) -> None:
        """그날 후보를 저장한다. 이미 고른 값이 있으면 건드리지 않는다."""
        day = self.days.setdefault(run_date, {})
        entry = day.setdefault(kind, {"candidates": [], "pick": None, "views": None})
        entry["candidates"] = [c for c in candidates if c]

    def log_pick(self, run_date: str, kind: str, pick: int, views: int | None = None,
                 title: str | None = None) -> dict:
        """고른 번호(1부터)와 조회수를 적는다. 후보 밖 번호면 title 을 같이 줘야 한다."""
        day = self.days.setdefault(run_date, {})
        entry = day.setdefault(kind, {"candidates": [], "pick": None, "views": None})
        cands = entry["candidates"]
        if 1 <= pick <= len(cands):
            entry["title"] = cands[pick - 1]
        elif title:
            entry["title"] = title
        else:
            raise ValueError(f"{run_date} {kind} 후보는 {len(cands)}개입니다. 번호를 확인하세요.")
        entry["pick"] = pick
        if views is not None:
            entry["views"] = int(views)
        entry["type"] = title_type(entry["title"])
        return entry

    def picked(self) -> list[dict]:
        rows = []
        for d, kinds in sorted(self.days.items(), reverse=True):
            for kind, e in kinds.items():
                if e.get("pick"):
                    rows.append({"date": d, "kind": kind, **e})
        return rows

    def by_type(self) -> dict[str, dict]:
        """유형별 개수·평균 조회수 (조회수가 있는 것만 평균에 넣는다)."""
        out: dict[str, dict] = {}
        for r in self.picked():
            t = r.get("type") or title_type(r.get("title", ""))
            slot = out.setdefault(t, {"count": 0, "views": [], "avg_views": None})
            slot["count"] += 1
            if r.get("views") is not None:
                slot["views"].append(r["views"])
        for slot in out.values():
            slot["avg_views"] = round(sum(slot["views"]) / len(slot["views"])) if slot["views"] else None
            del slot["views"]
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "days": self.days}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
