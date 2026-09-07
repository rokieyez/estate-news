"""주간 결산 — 일주일치 data.json 을 묶어 글 한 편을 더 만든다.

  output/weekly/2026-W36/weekly.md          마크다운
  output/weekly/2026-W36/weekly-naver.html  네이버 붙여넣기용
  output/weekly/2026-W36/data.json          이번 주에 들어간 하루치 요약 모음

LLM 호출은 1회. 키가 없으면 프롬프트 팩만 남긴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .llm import ContentGenerator, LLMError, Usage
from .models import BlogPost, WeeklyReview
from .prompts import build_weekly_prompt_pack
from .render import Renderer, copy_stats_images, update_index
from .store import CostLog

log = logging.getLogger(__name__)

WEEKLY_DIR = "weekly"


@dataclass
class WeeklyResult:
    week: str
    start: str
    end: str
    out_dir: Path
    days: int = 0
    files: list[Path] = field(default_factory=list)
    usage: Usage | None = None
    llm_used: bool = False
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False          # 자료가 모자라 일부러 안 만든 경우 (실패가 아니다)


def week_label(end: date) -> str:
    """ISO 주차. 2026-09-06(일) → '2026-W36'."""
    iso = end.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def collect_week(cfg: Config, end: date, days: int = 7) -> list[dict]:
    """end 를 포함해 지난 days 일의 data.json 을 날짜순으로 모은다. 없는 날은 건너뛴다."""
    rows: list[dict] = []
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        path = cfg.output_dir / day.isoformat() / "data.json"
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("%s 를 읽지 못했습니다: %s", path, exc)
            continue
        payload.setdefault("date", day.isoformat())
        rows.append(payload)
    mark_streaks(rows)
    return rows


STREAK_SIMILARITY = 0.35   # 데일리 클러스터링과 같은 임계값


def mark_streaks(days: list[dict]) -> list[dict]:
    """여러 날 반복된 이슈를 찾아 각 이슈에 days_seen 을, 첫날 항목에 streaks 를 단다.

    데일리는 seen.json 으로 3일 재탕을 막지만, 주간 결산은 7일치를 그대로 세면
    같은 사건을 두세 번 세게 된다. 제목 유사도로 묶어 '흐름' 으로 넘겨준다.
    """
    from .cluster import similarity

    groups: list[dict] = []          # {"title": 대표 제목, "dates": [...], "members": [(day_idx, issue)]}
    for di, day in enumerate(days):
        for issue in day.get("issues", []):
            title = issue.get("title") or ""
            if not title:
                continue
            for g in groups:
                if similarity(title, g["title"]) >= STREAK_SIMILARITY:
                    if day["date"] not in g["dates"]:
                        g["dates"].append(day["date"])
                    g["members"].append(issue)
                    break
            else:
                groups.append({"title": title, "dates": [day["date"]], "members": [issue]})
    streaks = []
    for g in groups:
        if len(g["dates"]) < 2:
            continue
        for issue in g["members"]:
            issue["days_seen"] = list(g["dates"])
        streaks.append({"title": g["title"], "dates": list(g["dates"])})
    if days:
        days[0]["streaks"] = streaks
    return streaks


def run_weekly(cfg: Config, *, end_date: str | None = None, use_llm: bool | None = None,
               min_days: int = 3) -> WeeklyResult:
    end = date.fromisoformat(end_date) if end_date else date.today()
    label = week_label(end)
    out_dir = cfg.output_dir / WEEKLY_DIR / label
    start = end - timedelta(days=6)
    result = WeeklyResult(week=label, start=start.isoformat(), end=end.isoformat(), out_dir=out_dir)

    days = collect_week(cfg, end)
    result.days = len(days)
    if len(days) < min_days:
        result.skipped = True
        result.warnings.append(
            f"지난 7일 중 브리핑이 {len(days)}일치뿐이라 결산을 만들지 않았습니다 (최소 {min_days}일)."
        )
        return result

    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = Renderer(cfg, out_dir, label)
    renderer._write_raw("data.json", json.dumps(
        {"week": label, "start": result.start, "end": result.end, "days": days},
        ensure_ascii=False, indent=2) + "\n")

    want_llm = cfg.llm_enabled if use_llm is None else (use_llm and bool(cfg.api_key))
    if not want_llm:
        if use_llm is not False and not cfg.api_key:
            result.warnings.append("ANTHROPIC_API_KEY 가 없어 결산 글을 건너뛰었습니다. weekly-prompt-pack.md 를 사용하세요.")
        renderer._write_raw("weekly-prompt-pack.md", build_weekly_prompt_pack(cfg, days, label))
        result.files = list(renderer.written)
        return result

    generator = ContentGenerator(cfg)
    result.usage = generator.usage
    try:
        review = generator.generate_weekly(days, label)
    except LLMError as exc:
        log.error("주간 결산 생성 실패: %s", exc)
        result.warnings.append(f"주간 결산 생성 실패 — {exc}")
        renderer._write_raw("weekly-prompt-pack.md", build_weekly_prompt_pack(cfg, days, label))
        result.files = list(renderer.written)
        return result

    result.llm_used = True
    _render_weekly(cfg, renderer, review, result)
    _weekly_images(cfg, renderer, result)
    _record_cost(cfg, result)
    result.warnings.extend(generator.usage.notes)
    index = update_index(cfg)
    result.files = list(renderer.written) + ([index] if index else [])
    return result


def _weekly_trades(cfg: Config, end: str) -> dict:
    """그 주의 실거래 요약. 날마다 쌓아 둔 장부를 읽을 뿐이라 API 를 다시 부르지 않는다."""
    from .store import TradeLog

    return TradeLog(cfg.state_dir / "trades.json").week_summary(end)


def _render_weekly(cfg: Config, renderer: Renderer, review: WeeklyReview, result: WeeklyResult) -> None:
    renderer._write(
        "weekly.md", "weekly.md.j2",
        review=review, week=result.week, start=result.start, end=result.end,
        trades=_weekly_trades(cfg, result.end),
        stats_images=copy_stats_images(cfg, renderer.out_dir, result.end),
        category=(cfg.get("blog", {}) or {}).get("category", "부동산"),
        disclaimer=(cfg.get("blog", {}) or {}).get("disclaimer", ""),
    )
    if str(cfg.get("blog.platform", "naver")).lower() == "naver":
        # 네이버 템플릿은 BlogPost 를 기대하므로 모양만 맞춰 준다.
        five = "\n".join(f"{i}. {line}" for i, line in enumerate(review.five_lines, start=1))
        post = BlogPost(
            title=review.title, slug=review.slug, meta_description=review.meta_description,
            tags=review.tags,
            body_markdown=f"**이번 주 다섯 줄**\n\n{five}\n\n{review.body_markdown}",
        )
        renderer.blog_naver(post, filename="weekly-naver.html")


def _record_cost(cfg: Config, result: WeeklyResult) -> None:
    if not (result.usage and result.usage.calls):
        return
    try:
        log_ = CostLog(cfg.state_dir / "costs.json")
        log_.record(result.end, result.usage, kind="weekly")
        log_.prune()
        log_.save()
    except OSError as exc:
        log.warning("비용 기록 실패: %s", exc)


def _weekly_images(cfg: Config, renderer: Renderer, result: WeeklyResult, limit: int = 2) -> list[str]:
    """이번 주에 사흘 이상 나온 지표의 추이 그림. 시계열 저장소(state/datapoints.json)를 쓴다."""
    from . import images
    from .store import SeriesStore

    img_cfg = cfg.get("images", {}) or {}
    if not img_cfg.get("enabled", True):
        return []
    rows = [r for r in SeriesStore(cfg.state_dir / "datapoints.json").rows
            if result.start <= r.get("date", "") <= result.end]
    if not rows:
        return []
    # 지표별로 묶어 점이 많은 순. 같은 지표는 한 번만.
    made: list[str] = []
    seen: list[dict] = []
    for row in sorted(rows, key=lambda r: r["date"], reverse=True):
        if any(images._same_metric(row, s) for s in seen):
            continue
        pts = images.series_for(row, rows)
        if len(pts) < images.SERIES_MIN_POINTS:
            continue
        img = images.time_series(row, row["date"], {"history": rows})
        if img is None:
            continue
        seen.append(row)
        img.slug = f"time-series-{len(seen)}"
        made.append(renderer._write_image(img, img_cfg))
        if len(seen) >= limit:
            break
    if made:
        result.warnings.append(f"이번 주 추이 그림 {len(made)}장을 만들었습니다: " + ", ".join(made))
    return made

