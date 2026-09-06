"""전체 파이프라인 조립: 수집 → 묶기 → 점수 → 요약 → 산출물."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cluster import build_clusters
from .collect import FeedResult, collect
from .config import Config
from .llm import ContentGenerator, LLMError, Usage
from .models import Article, Cluster
from .prompts import build_prompt_pack
from .rank import score_clusters, select_issues
from .render import RenderStats, Renderer, update_index
from .linkcheck import check_links
from .store import CostLog, SeenStore, SeriesStore, load_raw, save_raw

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    date: str
    out_dir: Path
    articles: int = 0
    issues: int = 0
    files: list[Path] = field(default_factory=list)
    usage: Usage | None = None
    llm_used: bool = False
    warnings: list[str] = field(default_factory=list)


def local_now(cfg: Config) -> datetime:
    """설정된 시간대의 현재 시각. 실행 날짜 판정에 쓴다."""
    name = cfg.get("run.timezone", "Asia/Seoul")
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(name))
    except Exception:
        # tzdata 가 없는 환경에서도 한국 시간 기준은 유지한다.
        return datetime.now(timezone(timedelta(hours=9)))


def run(
    cfg: Config,
    *,
    run_date: str | None = None,
    use_llm: bool | None = None,
    limit: int | None = None,
) -> RunResult:
    now_local = local_now(cfg)
    date_str = run_date or now_local.strftime("%Y-%m-%d")
    out_dir = cfg.output_dir / date_str
    result = RunResult(date=date_str, out_dir=out_dir)

    # 1) 수집
    log.info("기사 수집 시작 — 피드 %d개", len(cfg.enabled_feeds))
    articles, feed_results = collect(cfg, now=datetime.now(timezone.utc))
    if limit:
        articles = articles[:limit]
    result.articles = len(articles)
    log.info("수집 완료 — %d건", len(articles))

    _warn_about_feeds(result, feed_results)
    if not articles:
        result.warnings.append(
            "수집된 기사가 없습니다. `python -m rebrief doctor` 로 피드 상태를 확인하세요."
        )
        return result

    # 2) 최근에 다룬 기사 제외
    seen = SeenStore(cfg.state_dir / "seen.json")
    fresh = seen.filter_new(articles, int(cfg.get("run.skip_recent_days", 3)))
    if len(fresh) < len(articles):
        log.info("최근 다룬 기사 %d건 제외", len(articles) - len(fresh))
    # 전부 걸러지면 재탕이라도 내보내는 편이 낫다.
    working = fresh or articles

    # 3) 묶기 + 점수
    clusters = score_clusters(cfg, build_clusters(cfg, working), now=datetime.now(timezone.utc))
    issues = select_issues(cfg, clusters)
    result.issues = len(issues)
    log.info("이슈 %d개 선정 (전체 클러스터 %d개)", len(issues), len(clusters))

    # 4) 원본 보관
    if cfg.get("output.keep_raw", True):
        save_raw(
            out_dir / "raw" / "articles.json",
            articles,
            meta={
                "date": date_str,
                "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "feeds": [
                    {"id": r.feed.id, "ok": r.ok, "count": len(r.articles), "error": r.error}
                    for r in feed_results
                ],
            },
        )

    # 5) 산출물
    stats = _stats(articles, feed_results)
    renderer = Renderer(cfg, out_dir, date_str)
    chosen_ids = {a.id for c in issues for a in c.articles}
    leftovers = [a for a in working if a.id not in chosen_ids][:40]

    renderer.sources(
        issues,
        stats,
        [{"id": r.feed.id, "error": r.error} for r in feed_results if not r.ok],
        leftovers,
        link_status=_check_issue_links(cfg, issues, result),
    )

    want_llm = cfg.llm_enabled if use_llm is None else (use_llm and bool(cfg.api_key))
    if want_llm and issues:
        _generate_with_llm(cfg, renderer, issues, date_str, result)
    else:
        if use_llm is not False and not cfg.api_key:
            result.warnings.append(
                "ANTHROPIC_API_KEY 가 없어 요약을 건너뛰었습니다. prompt-pack.md 를 사용하세요."
            )
        renderer.brief_fallback(issues, stats)
        renderer.prompt_pack(build_prompt_pack(cfg, issues, date_str))

    # 6) 이력 저장
    seen.mark(articles, date_cls.fromisoformat(date_str))
    seen.prune(keep_days=30)
    seen.save()
    _record_cost(cfg, result)

    index = update_index(cfg)
    result.files = list(renderer.written) + ([index] if index else [])
    return result


def rerender(cfg: Config, run_date: str, *, use_llm: bool | None = None) -> RunResult:
    """보관된 raw/articles.json 으로 산출물만 다시 만든다 (재수집 없음)."""
    out_dir = cfg.output_dir / run_date
    raw_path = out_dir / "raw" / "articles.json"
    if not raw_path.exists():
        raise FileNotFoundError(f"보관된 수집 원본이 없습니다: {raw_path}")

    articles, meta = load_raw(raw_path)
    result = RunResult(date=run_date, out_dir=out_dir, articles=len(articles))

    clusters = score_clusters(cfg, build_clusters(cfg, articles))
    issues = select_issues(cfg, clusters)
    result.issues = len(issues)

    feed_meta = meta.get("feeds", []) or []
    stats = RenderStats(
        articles=len(articles),
        publishers=len({a.publisher or a.feed_name for a in articles}),
        feeds_ok=sum(1 for f in feed_meta if f.get("ok")),
        feeds_total=len(feed_meta),
    )

    renderer = Renderer(cfg, out_dir, run_date)
    chosen_ids = {a.id for c in issues for a in c.articles}
    renderer.sources(
        issues,
        stats,
        [{"id": f.get("id", "?"), "error": f.get("error")} for f in feed_meta if not f.get("ok")],
        [a for a in articles if a.id not in chosen_ids][:40],
        link_status=_check_issue_links(cfg, issues, result),
    )

    want_llm = cfg.llm_enabled if use_llm is None else (use_llm and bool(cfg.api_key))
    if want_llm and issues:
        _generate_with_llm(cfg, renderer, issues, run_date, result)
    else:
        renderer.brief_fallback(issues, stats)
        renderer.prompt_pack(build_prompt_pack(cfg, issues, run_date))

    _record_cost(cfg, result)
    index = update_index(cfg)
    result.files = list(renderer.written) + ([index] if index else [])
    return result


# ── 내부 ─────────────────────────────────────────────────────


def _generate_with_llm(
    cfg: Config,
    renderer: Renderer,
    issues: list[Cluster],
    date_str: str,
    result: RunResult,
) -> None:
    """LLM 3단계 생성. 중간에 실패해도 거기까지 만든 건 남긴다."""
    generator = ContentGenerator(cfg)
    result.usage = generator.usage

    try:
        brief = generator.generate_brief(issues, date_str)
    except LLMError as exc:
        log.error("브리핑 생성 실패: %s", exc)
        result.warnings.append(f"브리핑 생성 실패 — {exc}")
        renderer.brief_fallback(issues, _stats_from_clusters(issues))
        renderer.prompt_pack(build_prompt_pack(cfg, issues, date_str))
        return

    result.llm_used = True
    renderer.brief(brief, _stats_from_clusters(issues))
    renderer.data_json(brief)
    history = _record_series(cfg, brief, date_str)

    # 그림은 블로그 글의 이미지 자리에 맞춰 만들어야 하므로 글을 먼저 받는다.
    # 글 생성이 실패하면 자리 정보 없이 수치만 보고 만든다.
    post = None
    try:
        post = generator.generate_blog(brief)
    except LLMError as exc:
        log.error("블로그 생성 실패: %s", exc)
        result.warnings.append(f"블로그 생성 실패 — {exc}")

    slot_files = renderer.images(brief, history=history, post=post)
    if post is not None:
        renderer.blog(post, issues, slot_files)
        if str(cfg.get("blog.platform", "naver")).lower() == "naver":
            renderer.blog_naver(post, slot_files)

    try:
        pack = generator.generate_video(brief)
    except LLMError as exc:
        log.error("영상 대본 생성 실패: %s", exc)
        result.warnings.append(f"영상 대본 생성 실패 — {exc}")
        return

    renderer.shorts(pack)
    renderer.longform(pack)
    renderer.production_notes(brief, pack)
    renderer.thumbnails(pack)
    renderer.shorts_draft(pack)
    result.warnings.extend(generator.usage.notes)


def _stats(articles: list[Article], feed_results: list[FeedResult]) -> RenderStats:
    return RenderStats(
        articles=len(articles),
        publishers=len({a.publisher or a.feed_name for a in articles}),
        feeds_ok=sum(1 for r in feed_results if r.ok),
        feeds_total=len(feed_results),
    )


def _stats_from_clusters(clusters: list[Cluster]) -> RenderStats:
    articles = [a for c in clusters for a in c.articles]
    return RenderStats(
        articles=len(articles),
        publishers=len({a.publisher or a.feed_name for a in articles}),
        feeds_ok=0,
        feeds_total=0,
    )


def _warn_about_feeds(result: RunResult, feed_results: list[FeedResult]) -> None:
    failed = [r for r in feed_results if not r.ok]
    if not failed:
        return
    names = ", ".join(r.feed.id for r in failed)
    result.warnings.append(f"피드 {len(failed)}개 실패: {names}")


# ── 부가 단계 ────────────────────────────────────────────────


def _check_issue_links(cfg: Config, issues: list[Cluster], result: RunResult) -> dict:
    """선정된 이슈의 기사 링크만 점검한다. 꺼져 있으면 빈 dict."""
    if not cfg.get("collect.check_links", True):
        return {}
    cap = int(cfg.get("collect.check_links_max", 60))
    urls = [a.url for c in issues for a in c.articles][:cap]
    try:
        status = check_links(cfg, urls)
    except Exception as exc:                 # 링크 점검이 파이프라인을 세우면 안 된다
        log.warning("링크 점검 실패: %s", exc)
        return {}
    dead = [u for u, st in status.items() if not st.ok]
    if dead:
        result.warnings.append(f"출처 링크 {len(dead)}개가 열리지 않습니다 (sources.md 에 표시)")
    return status


def _record_cost(cfg: Config, result: RunResult, *, kind: str = "daily") -> None:
    if not (result.usage and result.usage.calls):
        return
    try:
        log_ = CostLog(cfg.state_dir / "costs.json")
        log_.record(result.date, result.usage, kind=kind)
        log_.prune()
        log_.save()
    except OSError as exc:
        log.warning("비용 기록 실패: %s", exc)


def _record_series(cfg: Config, brief, date_str: str) -> list[dict]:
    """오늘 수치를 시계열에 넣고, 이미지 단계가 쓸 전체 이력을 돌려준다."""
    from .render import flatten_datapoints

    store = SeriesStore(cfg.state_dir / "datapoints.json")
    try:
        store.record(date_str, flatten_datapoints(brief))
        store.prune()
        store.save()
    except OSError as exc:
        log.warning("수치 이력 기록 실패: %s", exc)
    return store.rows

