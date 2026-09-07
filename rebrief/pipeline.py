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
from . import keynumbers
from .llm import ContentGenerator, LLMError, Usage
from .models import Article, Cluster
from .prompts import build_prompt_pack
from .rank import score_clusters, select_issues
from .render import RenderStats, Renderer, explain_issues, update_index
from .linkcheck import check_links
from .store import CostLog, SeenStore, SeriesStore, load_raw, recent_topics, save_raw

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

    link_status = renderer.last_link_status
    want_llm = cfg.llm_enabled if use_llm is None else (use_llm and bool(cfg.api_key))
    model = _budget_guard(cfg, result) if want_llm else None
    if want_llm and model == "":
        want_llm = False                      # 월 예산 초과
    artifacts: dict = {}
    if want_llm and issues:
        artifacts = _generate_with_llm(cfg, renderer, issues, date_str, result, model=model)
    else:
        if use_llm is not False and not cfg.api_key:
            result.warnings.append(
                "ANTHROPIC_API_KEY 가 없어 요약을 건너뛰었습니다. prompt-pack.md 를 사용하세요."
            )
        renderer.brief_fallback(issues, stats)
        renderer.prompt_pack(build_prompt_pack(cfg, issues, date_str))
    renderer.checklist(result, artifacts, link_status)

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

    link_status = renderer.last_link_status
    want_llm = cfg.llm_enabled if use_llm is None else (use_llm and bool(cfg.api_key))
    model = _budget_guard(cfg, result) if want_llm else None
    if want_llm and model == "":
        want_llm = False
    artifacts: dict = {}
    if want_llm and issues:
        artifacts = _generate_with_llm(cfg, renderer, issues, run_date, result, model=model)
    else:
        renderer.brief_fallback(issues, stats)
        renderer.prompt_pack(build_prompt_pack(cfg, issues, run_date))
    renderer.checklist(result, artifacts, link_status)

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
    model: str | None = None,
) -> dict:
    """LLM 3단계 생성. 중간에 실패해도 거기까지 만든 건 남긴다.

    점검표가 쓸 수 있게 만든 것들(brief/post/pack/checks)을 dict 로 돌려준다.
    """
    generator = ContentGenerator(cfg, model=model)
    result.usage = generator.usage
    made: dict = {}

    try:
        brief = generator.generate_brief(issues, date_str)
    except LLMError as exc:
        log.error("브리핑 생성 실패: %s", exc)
        result.warnings.append(f"브리핑 생성 실패 — {exc}")
        renderer.brief_fallback(issues, _stats_from_clusters(issues))
        renderer.prompt_pack(build_prompt_pack(cfg, issues, date_str))
        return made

    result.llm_used = True
    checks = _verify_numbers(cfg, brief, issues, result)
    made.update(brief=brief, checks=checks)
    made["repeats"] = _repeat_topics(cfg, brief, date_str)
    renderer.brief(brief, _stats_from_clusters(issues), checks,
                   diff=_diff_yesterday(cfg, brief, date_str),
                   why=explain_issues(brief, issues, str(cfg.get("run.timezone", "Asia/Seoul"))))
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
    keys: list = []
    if post is not None:
        # 오늘의 핵심 수치: 브리핑 datapoint 가운데 글에 실제로 쓰인 것. 블로그 카드·강조·썸네일 배지가 함께 쓴다.
        keys = keynumbers.pick(brief, post.body_markdown, int(cfg.get("blog.key_numbers", 3) or 0))
        made.update(post=post, key_numbers=keys, empty_photo_slots=sum(
            1 for i in range(1, len(post.image_slots) + 1) if i not in slot_files))
        renderer.blog(post, issues, slot_files, key_numbers=keys)
        if str(cfg.get("blog.platform", "naver")).lower() == "naver":
            renderer.blog_naver(post, slot_files, key_numbers=keys)
        _record_titles(cfg, date_str, blog=[post.title])

    try:
        pack = generator.generate_video(brief)
    except LLMError as exc:
        log.error("영상 대본 생성 실패: %s", exc)
        result.warnings.append(f"영상 대본 생성 실패 — {exc}")
        return made

    made["pack"] = pack
    renderer.shorts(pack)
    renderer.longform(pack)
    renderer.production_notes(brief, pack)
    renderer.thumbnails(pack, key_numbers=keys)
    _record_titles(cfg, date_str, longform=pack.longform.title_candidates,
                   shorts=pack.shorts.title_candidates)
    _autofix_banned(cfg, renderer, made, issues, slot_files, result)
    result.warnings.extend(generator.usage.notes)
    return made


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


def _verify_numbers(cfg: Config, brief, issues: list[Cluster], result: RunResult) -> list:
    """브리핑 수치를 기사 원문과 대조한다. 미확인이 있으면 경고에 올린다."""
    if not cfg.get("verify.numbers", True):
        return []
    from .verify import NOT_FOUND, check_numbers

    checks = check_numbers(brief, issues)
    missing = [c for c in checks if c.status == NOT_FOUND]
    if missing:
        result.warnings.append(
            f"수치 {len(missing)}건이 기사 원문에서 확인되지 않았습니다 (brief.md '숫자 검산' 참고)"
        )
    return checks


def _record_titles(cfg: Config, date_str: str, **kinds: list[str]) -> None:
    """그날 제목 후보를 장부에 남긴다. 나중에 무엇을 골랐는지 적을 수 있게."""
    from .store import TitleLog

    try:
        log_ = TitleLog(cfg.state_dir / "titles.json")
        for kind, candidates in kinds.items():
            if candidates:
                log_.record_candidates(date_str, kind, list(candidates))
        log_.save()
    except OSError as exc:
        log.warning("제목 기록 실패: %s", exc)


def _budget_guard(cfg: Config, result: RunResult) -> str | None:
    """월 예산에 따라 쓸 모델을 정한다. None = 기본, 대체 모델명 = 절약, "" = 이번 실행 건너뜀."""
    budget = float(cfg.get("llm.monthly_budget_usd", 0) or 0)
    if budget <= 0:
        return None
    spent = CostLog(cfg.state_dir / "costs.json").this_month()
    fallback = str(cfg.get("llm.fallback_model", "") or "").strip()
    if spent >= budget:
        result.warnings.append(
            f"이번 달 비용 ${spent:.2f} 이 예산 ${budget:.2f} 을 넘어 요약을 건너뛰었습니다 (settings.yaml llm.monthly_budget_usd)."
        )
        return ""
    ratio = float(cfg.get("llm.budget_soft_ratio", 0.8) or 0.8)
    if fallback and spent >= budget * ratio and fallback != cfg.get("llm.model"):
        result.warnings.append(
            f"이번 달 비용 ${spent:.2f} 이 예산의 {ratio:.0%} 를 넘어 {fallback} 로 생성했습니다."
        )
        return fallback
    return None


def _repeat_topics(cfg: Config, brief, run_date: str, threshold: float = 0.5) -> list[dict]:
    """오늘 이슈가 최근 며칠 안에 이미 다룬 주제인지 본다. 제목 2-gram Dice 로 비교한다."""
    from datetime import date as _date

    from .cluster import similarity

    try:
        today = _date.fromisoformat(run_date)
    except ValueError:
        today = None
    history = recent_topics(cfg.output_dir, today, days=int(cfg.get("run.repeat_lookback_days", 7)))
    if not history or brief is None:
        return []
    out: list[dict] = []
    for issue in brief.issues:
        best = max(history, key=lambda h: similarity(issue.title, h[1]), default=None)
        if best is None or similarity(issue.title, best[1]) < threshold:
            continue
        days_ago = (today - _date.fromisoformat(best[0])).days if today else 0
        out.append({"title": issue.title, "prev_date": best[0], "prev_title": best[1],
                    "days_ago": max(days_ago, 1)})
    return out


def _diff_yesterday(cfg: Config, brief, date_str: str) -> dict:
    """가장 최근 이전 날짜의 data.json 과 이슈 제목을 견줘 새것/이어지는 것/사라진 것을 나눈다."""
    import json

    from .cluster import similarity

    out_dir = cfg.output_dir
    prev = None
    if out_dir.exists():
        for p in sorted((d for d in out_dir.iterdir() if d.is_dir() and d.name < date_str
                         and len(d.name) == 10), reverse=True):
            if (p / "data.json").exists():
                prev = p
                break
    if prev is None:
        return {}
    try:
        yesterday = json.loads((prev / "data.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    old_titles = [i.get("title", "") for i in yesterday.get("issues", [])]
    new, cont = [], []
    for issue in brief.issues:
        match = next((t for t in old_titles if similarity(issue.title, t) >= 0.35), None)
        (cont if match else new).append(issue.title)
    gone = [t for t in old_titles if not any(similarity(t, i.title) >= 0.35 for i in brief.issues)]
    return {"date": prev.name, "new": new, "continuing": cont, "gone": gone}


def _autofix_banned(cfg: Config, renderer: Renderer, made: dict, issues: list[Cluster],
                    slot_files: dict, result: RunResult) -> None:
    """금지 표현이 든 문장만 저렴한 모델로 고쳐 쓰고 해당 산출물을 다시 쓴다."""
    from . import checklist as cl

    model = str(cfg.get("checklist.autofix_model", "") or "").strip()
    phrases = [b for b in ((cfg.get("video", {}) or {}).get("banned_phrases", []) or []) if b]
    if not model or not phrases:
        return
    post, pack = made.get("post"), made.get("pack")
    targets = []
    if post is not None:
        targets.append(("블로그", post.body_markdown))
    if pack is not None:
        targets.append(("쇼츠", "\n".join(l.text for l in pack.shorts.lines)))
        targets.append(("롱폼", "\n".join(s.script for s in pack.longform.sections)))
    if not any(cl.sentences_with(t, phrases) for _, t in targets):
        return

    gen = ContentGenerator(cfg, model=model)
    tone = str((cfg.get("video", {}) or {}).get("tone", "") or "")
    fixed: list[str] = []
    if post is not None:
        post.body_markdown, ch = cl.autofix(post.body_markdown, phrases, lambda s, p: gen.rewrite(s, p, tone))
        if ch:
            fixed += [f"블로그: {a} → {b}" for a, b in ch]
            keys = made.get("key_numbers") or []
            renderer.blog(post, issues, slot_files, key_numbers=keys)
            if str(cfg.get("blog.platform", "naver")).lower() == "naver":
                renderer.blog_naver(post, slot_files, key_numbers=keys)
    if pack is not None:
        changed = False
        for line in pack.shorts.lines:
            line.text, ch = cl.autofix(line.text, phrases, lambda s, p: gen.rewrite(s, p, tone))
            if ch:
                changed = True
                fixed += [f"쇼츠: {a} → {b}" for a, b in ch]
        for sec in pack.longform.sections:
            sec.script, ch = cl.autofix(sec.script, phrases, lambda s, p: gen.rewrite(s, p, tone))
            if ch:
                changed = True
                fixed += [f"롱폼: {a} → {b}" for a, b in ch]
        if changed:
            renderer.shorts(pack)
            renderer.longform(pack)
    if fixed:
        result.warnings.append(f"금지 표현이 든 문장 {len(fixed)}개를 {model} 로 고쳐 썼습니다 (checklist.md 에 전후 기록)")
        made["autofixed"] = fixed
    if result.usage is not None:
        # 고쳐 쓰기 비용도 그날 장부에 합산한다
        u = gen.usage
        result.usage.calls += u.calls
        result.usage.input_tokens += u.input_tokens
        result.usage.output_tokens += u.output_tokens
        result.usage._usd += u.estimated_usd
        for m in u.models_used:
            if m not in result.usage.models_used:
                result.usage.models_used.append(m)

