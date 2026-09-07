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
    """수집 원본을 남겨 둔다. render 명령으로 언제든 재생성할 수 있다.

    raw/articles.json 은 기사 본문이 들어 있어 커밋하지 않는다(.gitignore). 대시보드가 쓰는
    건수·피드 상태만 글자 없이 날짜 폴더의 collect.json 에 따로 적어 저장소에 남긴다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "articles": [json.loads(a.model_dump_json()) for a in articles],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = {**meta, "articles": len(articles)}
    (path.parent.parent / "collect.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        details = list(getattr(usage, "details", []) or [])
        if details:
            entry["by_call"] = details          # 어느 단계에서 돈이 나갔는지 (모델 분리 판단용)
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


def recent_topics(output_dir: Path, today: date | None = None, days: int = 7,
                  skip_today: bool = True) -> list[tuple[str, str]]:
    """최근 며칠간 다룬 이슈 제목을 (날짜, 제목) 으로 모은다. 같은 주제를 또 쓰는지 볼 때 쓴다."""
    today = today or date.today()
    out: list[tuple[str, str]] = []
    for i in range(0 if not skip_today else 1, days + 1):
        day = (today - timedelta(days=i)).isoformat()
        path = output_dir / day / "data.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for issue in data.get("issues", []) or []:
            title = (issue.get("title") or "").strip()
            if title:
                out.append((day, title))
    return out


class PublishLog:
    """어느 날 글을 실제로 발행했는지, 주소는 무엇인지 적는 장부."""

    def __init__(self, path: Path):
        self.path = path
        self.days: dict[str, dict] = {}
        if path.exists():
            try:
                self.days = json.loads(path.read_text(encoding="utf-8")).get("days", {}) or {}
            except (json.JSONDecodeError, OSError):
                self.days = {}

    def record(self, run_date: str, url: str = "", note: str = "", views: int | None = None) -> dict:
        entry = self.days.setdefault(run_date, {})
        entry["at"] = datetime.now().isoformat(timespec="seconds")
        if url:
            entry["url"] = url.strip()
        if note:
            entry["note"] = note.strip()
        if views is not None:
            entry["views"] = int(views)
        return entry

    def get(self, run_date: str) -> dict:
        return self.days.get(run_date, {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "days": self.days}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


TRADES_FILE = "trades.json"


class TradeLog:
    """날마다 집계한 실거래 결과를 남긴다. 며칠 쌓이면 우리가 만든 추이가 된다.

    같은 달을 여러 날 집계하면 신고가 늦게 들어와 값이 조금씩 커진다. 그래서
    '집계한 날' 기준으로 남기고, 같은 날 다시 돌리면 덮어쓴다.
    """

    def __init__(self, path: Path):
        self.path = path
        self.days: dict[str, dict] = {}
        if path.exists():
            try:
                self.days = json.loads(path.read_text(encoding="utf-8")).get("days", {}) or {}
            except (json.JSONDecodeError, OSError):
                self.days = {}

    def add(self, run_date: str, data: dict, series: dict | None = None) -> None:
        if not data or not data.get("districts"):
            return
        self.days[run_date] = {
            "month": data.get("month", ""),
            "total": data.get("total", 0),
            "districts": {r["name"]: {"count": r["now"]["count"], "avg": r["now"]["avg"]}
                          for r in data["districts"]},
            "index": {name: (rows[-1]["value"] if rows else None)
                      for name, rows in (series or {}).items()},
        }

    def month_series(self, name: str, limit: int = 12) -> list[dict]:
        """한 지역의 집계일별 거래 건수. 같은 달을 여러 번 집계한 것도 그대로 남긴다."""
        rows = []
        for day, entry in sorted(self.days.items()):
            got = (entry.get("districts") or {}).get(name)
            if got:
                rows.append({"date": day, "month": entry.get("month", ""), **got})
        return rows[-limit:]

    def week_summary(self, end: str, days: int = 7) -> dict:
        """지난 며칠치 집계에서 주간 결산에 쓸 것만 뽑는다. API 를 다시 부르지 않는다."""
        from datetime import date as _date
        from datetime import timedelta as _td

        try:
            last = _date.fromisoformat(end)
        except ValueError:
            return {}
        wanted = {(last - _td(days=i)).isoformat() for i in range(days)}
        rows = [(day, entry) for day, entry in sorted(self.days.items()) if day in wanted]
        if not rows:
            return {}
        first_day, first = rows[0]
        last_day, latest = rows[-1]
        index = {}
        for name, value in (latest.get("index") or {}).items():
            before = (first.get("index") or {}).get(name)
            if value is not None:
                index[name] = {"value": value,
                               "change": round(value - before, 2) if before is not None else None}
        return {
            "from": first_day, "to": last_day, "month": latest.get("month", ""),
            "total": latest.get("total", 0),
            "total_change": latest.get("total", 0) - first.get("total", 0) if len(rows) > 1 else 0,
            "districts": sorted(
                ({"name": n, **v} for n, v in (latest.get("districts") or {}).items()),
                key=lambda r: r.get("count", 0), reverse=True)[:5],
            "index": index,
            "days": len(rows),
        }

    def prune(self, keep: int = 180) -> None:
        if len(self.days) <= keep:
            return
        self.days = dict(sorted(self.days.items())[-keep:])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "days": self.days}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


POLICIES_FILE = "policies.json"


class PolicyLog:
    """지금까지 실은 정부 발표를 기억한다. 같은 정책의 후속 발표를 이어 주고, 앞으로의 일정을 모은다."""

    def __init__(self, path: Path):
        self.path = path
        self.docs: dict[str, dict] = {}
        if path.exists():
            try:
                self.docs = json.loads(path.read_text(encoding="utf-8")).get("docs", {}) or {}
            except (json.JSONDecodeError, OSError):
                self.docs = {}

    def add(self, doc, run_date: str, schedule: list[dict] | None = None) -> None:
        self.docs[str(doc.news_id)] = {
            "title": doc.title, "dept": doc.dept, "date": doc.date or run_date,
            "url": doc.url, "summary": list(doc.summary or [])[:3],
            "schedule": list(schedule or []), "seen_on": run_date,
        }

    def follow_ups(self, doc, threshold: float = 0.45, limit: int = 3) -> list[dict]:
        """제목이 비슷한 지난 발표. 같은 정책이 며칠에 걸쳐 여러 번 나오는 걸 이어 준다."""
        from .cluster import similarity

        found = []
        for news_id, row in self.docs.items():
            if news_id == str(doc.news_id):
                continue
            score = similarity(doc.title, row.get("title", ""))
            if score >= threshold:
                found.append({**row, "news_id": news_id, "score": round(score, 3)})
        found.sort(key=lambda r: (r.get("date", ""), r["score"]), reverse=True)
        return found[:limit]

    def upcoming(self, after: str, limit: int = 8) -> list[dict]:
        """오늘 이후의 일정만 날짜순으로. 같은 날 같은 내용은 한 번만."""
        rows, seen = [], set()
        for row in self.docs.values():
            for item in row.get("schedule", []) or []:
                when = str(item.get("date", ""))
                key = (when, str(item.get("text", ""))[:24])
                if when < after or key in seen:
                    continue
                seen.add(key)
                rows.append(item)
        rows.sort(key=lambda r: r["date"])
        return rows[:limit]

    def prune(self, keep: int = 120) -> None:
        """오래된 것부터 버린다. 장부가 끝없이 커지지 않게."""
        if len(self.docs) <= keep:
            return
        ordered = sorted(self.docs.items(), key=lambda kv: kv[1].get("seen_on", ""), reverse=True)
        self.docs = dict(ordered[:keep])

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "docs": self.docs}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def previous_blog_bodies(output_dir: Path, run_date: str, days: int = 3) -> list[tuple[str, str]]:
    """최근 며칠치 blog.md 본문. 머리말과 '참고한 기사' 목록은 뺀다."""
    try:
        today = date.fromisoformat(run_date)
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    for i in range(1, days + 1):
        day = (today - timedelta(days=i)).isoformat()
        path = output_dir / day / "blog.md"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.startswith("---"):
            parts = text.split("---", 2)
            text = parts[2] if len(parts) > 2 else text
        text = text.split("### 참고한 기사")[0]
        out.append((day, text))
    return out


# ── 연속 실패 ────────────────────────────────────────────────


def failure_streak(output_dir: Path, today: date | None = None, lookback: int = 14) -> int:
    """오늘부터 거꾸로, 요약(data.json)이 없는 날이 몇 일 연속인지. 폴더 자체가 없는 날도 실패로 센다."""
    today = today or date.today()
    # 프로젝트가 시작되기 전 날짜까지 실패로 세면 첫날부터 '14일 연속' 이 된다.
    existing = [p.name for p in output_dir.iterdir() if p.is_dir() and _parse_date(p.name)] if output_dir.exists() else []
    if not existing:
        return 0
    earliest = min(existing)
    streak = 0
    for i in range(lookback):
        d = (today - timedelta(days=i)).isoformat()
        if d < earliest or (output_dir / d / "data.json").exists():
            break
        streak += 1
    return streak

