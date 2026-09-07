"""월간 결산 — 한 달치 브리핑에 그달 확정된 실거래를 얹어 글 한 편을 더 만든다.

  output/monthly/2026-08/monthly.md          마크다운
  output/monthly/2026-08/monthly-naver.html  네이버 붙여넣기용
  output/monthly/2026-08/data.json           이 달에 들어간 하루치 요약 모음

주간 결산과 겹치지 않는 이유는 두 가지입니다.

* **실거래는 달 단위로만 확정됩니다.** 신고 기한 30일 탓에 확정된 달이 지금보다 두 달쯤
  앞서므로, 주 단위로는 "같은 달 숫자가 조금 커졌다" 밖에 말할 게 없습니다.
* **검색 수명이 다릅니다.** '2026년 8월 부동산' 은 몇 달 뒤에도 찾지만 'W36' 은 그 주가
  지나면 아무도 찾지 않습니다.

LLM 호출은 1회. 키가 없으면 프롬프트 팩만 남깁니다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .llm import ContentGenerator, LLMError, Usage
from .models import BlogPost, MonthlyReview
from .prompts import build_monthly_prompt_pack
from .render import Renderer, copy_stats_images, update_index
from .store import CostLog, TradeLog

log = logging.getLogger(__name__)

MONTHLY_DIR = "monthly"


@dataclass
class MonthlyResult:
    month: str                 # '2026-08'
    out_dir: Path
    days: int = 0
    files: list[Path] = field(default_factory=list)
    usage: Usage | None = None
    llm_used: bool = False
    warnings: list[str] = field(default_factory=list)
    skipped: bool = False      # 자료가 모자라 일부러 안 만든 경우 (실패가 아니다)


def prev_month_of(run_date: str) -> str:
    """실행일이 속한 달의 **바로 앞 달**. '2026-09-01' → '2026-08'.

    달이 끝나야 결산이므로 이달은 쓰지 않습니다. 매달 1일에 돌리면 자연히 지난달이 됩니다.
    """
    try:
        d = date.fromisoformat(run_date)
    except ValueError:
        d = date.today()
    return f"{d.year - 1}-12" if d.month == 1 else f"{d.year}-{d.month - 1:02d}"


def month_title(ym: str) -> str:
    """'2026-08' → '2026년 8월'."""
    try:
        year, month = ym.split("-")
        return f"{year}년 {int(month)}월"
    except (ValueError, AttributeError):
        return ym


def collect_month(cfg: Config, ym: str) -> list[dict]:
    """그 달의 data.json 을 날짜순으로 모은다. 없는 날은 건너뛴다."""
    rows: list[dict] = []
    for path in sorted(cfg.output_dir.glob(f"{ym}-*/data.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("%s 를 읽지 못했습니다: %s", path, exc)
            continue
        payload.setdefault("date", path.parent.name)
        rows.append(payload)
    return rows


def month_trades(cfg: Config, ym: str) -> dict:
    """그 달에 집계해 둔 실거래 장부. API 를 다시 부르지 않는다.

    장부는 **집계일** 기준이라 그 달의 마지막 집계가 그 달의 최종값입니다. 값 자체는
    두 달쯤 앞선 달(month)의 거래이므로, 결산 글에서는 어느 달 수치인지 반드시 밝힙니다.
    """
    book = TradeLog(cfg.state_dir / "trades.json")
    days = sorted(d for d in book.days if d.startswith(f"{ym}-"))
    if not days:
        return {}
    return book.week_summary(days[-1], days=len(days))


def run_monthly(cfg: Config, month: str | None = None, *, use_llm: bool | None = None,
                min_days: int = 10) -> MonthlyResult:
    """한 달치를 묶어 결산 글을 만든다. 브리핑이 너무 적은 달은 만들지 않는다."""
    ym = month or prev_month_of(date.today().isoformat())
    out_dir = cfg.output_dir / MONTHLY_DIR / ym
    result = MonthlyResult(month=ym, out_dir=out_dir)

    days = collect_month(cfg, ym)
    result.days = len(days)
    if len(days) < min_days:
        result.skipped = True
        result.warnings.append(
            f"{month_title(ym)} 브리핑이 {len(days)}일치뿐이라 결산을 만들지 않았습니다 (최소 {min_days}일)."
        )
        return result

    trades = month_trades(cfg, ym)
    out_dir.mkdir(parents=True, exist_ok=True)
    renderer = Renderer(cfg, out_dir, ym)
    renderer._write_raw("data.json", json.dumps(
        {"month": ym, "days": days, "trades": trades}, ensure_ascii=False, indent=2) + "\n")

    want_llm = cfg.llm_enabled if use_llm is None else (use_llm and bool(cfg.api_key))
    if not want_llm:
        if use_llm is not False and not cfg.api_key:
            result.warnings.append(
                "ANTHROPIC_API_KEY 가 없어 결산 글을 건너뛰었습니다. monthly-prompt-pack.md 를 사용하세요.")
        renderer._write_raw("monthly-prompt-pack.md",
                            build_monthly_prompt_pack(cfg, days, month_title(ym), trades))
        result.files = list(renderer.written)
        return result

    generator = ContentGenerator(cfg)
    result.usage = generator.usage
    try:
        review = generator.generate_monthly(days, month_title(ym), trades)
    except LLMError as exc:
        log.error("월간 결산 생성 실패: %s", exc)
        result.warnings.append(f"월간 결산 생성 실패 — {exc}")
        renderer._write_raw("monthly-prompt-pack.md",
                            build_monthly_prompt_pack(cfg, days, month_title(ym), trades))
        result.files = list(renderer.written)
        return result

    result.llm_used = True
    _render_monthly(cfg, renderer, review, result, trades)
    _record_cost(cfg, result)
    result.warnings.extend(generator.usage.notes)
    index = update_index(cfg)
    result.files = list(renderer.written) + ([index] if index else [])
    return result


def _render_monthly(cfg: Config, renderer: Renderer, review: MonthlyReview,
                    result: MonthlyResult, trades: dict) -> None:
    renderer._write(
        "monthly.md", "monthly.md.j2",
        review=review, month=result.month, month_label=month_title(result.month),
        days=result.days, trades=trades,
        stats_images=copy_stats_images(cfg, renderer.out_dir, f"{result.month}-28"),
        category=(cfg.get("blog", {}) or {}).get("category", "부동산"),
        disclaimer=(cfg.get("blog", {}) or {}).get("disclaimer", ""),
    )
    if str(cfg.get("blog.platform", "naver")).lower() == "naver":
        # 네이버 템플릿은 BlogPost 를 기대하므로 모양만 맞춰 준다.
        five = "\n".join(f"{i}. {line}" for i, line in enumerate(review.month_lines, start=1))
        post = BlogPost(
            title=review.title, slug=review.slug, meta_description=review.meta_description,
            tags=review.tags,
            body_markdown=f"**{month_title(result.month)} 다섯 줄**\n\n{five}\n\n{review.body_markdown}",
        )
        renderer.blog_naver(post, filename="monthly-naver.html")


def _record_cost(cfg: Config, result: MonthlyResult) -> None:
    if not (result.usage and result.usage.calls):
        return
    try:
        book = CostLog(cfg.state_dir / "costs.json")
        book.record(f"{result.month}-01", result.usage, kind="monthly")
        book.prune()
        book.save()
    except OSError as exc:
        log.warning("비용 기록 실패: %s", exc)
