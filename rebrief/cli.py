"""명령줄 인터페이스.

    python -m rebrief run          오늘치 전체 실행 (수집 → 요약 → 대본)
    python -m rebrief collect      수집만 하고 원본 저장
    python -m rebrief render       저장된 원본으로 산출물만 다시 생성
    python -m rebrief doctor       RSS 피드가 살아있는지 점검
    python -m rebrief notify       실행 결과를 텔레그램으로 보내기 (토큰이 있을 때)
    python -m rebrief weekly       지난 7일치를 묶은 주간 결산 글
    python -m rebrief titles       제목 후보 보기 / 실제로 고른 것과 조회수 기록
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from .collect import collect, fetch_feeds
from .config import load_config
from .pipeline import local_now, rerender, run as run_pipeline
from .store import save_raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rebrief",
        description="부동산 뉴스를 매일 수집해 블로그 글과 영상 대본으로 만듭니다.",
    )
    parser.add_argument("--config", help="설정 폴더 경로 (기본: config/)")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 로그")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="전체 파이프라인 실행")
    p_run.add_argument("--date", help="산출물 날짜 (기본: 오늘, YYYY-MM-DD)")
    p_run.add_argument("--no-llm", action="store_true", help="요약·대본 생성을 건너뜀")
    p_run.add_argument("--limit", type=int, help="기사 수 제한 (테스트용)")

    p_collect = sub.add_parser("collect", help="수집만 실행")
    p_collect.add_argument("--date", help="저장 날짜 (기본: 오늘)")

    p_render = sub.add_parser("render", help="저장된 원본으로 재생성")
    p_render.add_argument("--date", help="대상 날짜 (기본: 오늘)")
    p_render.add_argument("--no-llm", action="store_true", help="요약·대본 생성을 건너뜀")

    sub.add_parser("site", help="휴대폰에서 볼 사이트 만들기 (site/)")

    sub.add_parser("doctor", help="RSS 피드 상태 점검")

    p_notify = sub.add_parser("notify", help="실행 결과를 텔레그램으로 보내기")
    p_notify.add_argument("--date", help="대상 날짜 (기본: 오늘)")
    p_notify.add_argument("--failed", action="store_true", help="실패 알림을 보냄")
    p_notify.add_argument("--run-url", default="", help="Actions 실행 링크 (실패 알림에 붙임)")

    p_weekly = sub.add_parser("weekly", help="지난 7일치를 묶은 주간 결산 글")
    p_weekly.add_argument("--end", help="결산 마지막 날짜 (기본: 오늘, YYYY-MM-DD)")
    p_weekly.add_argument("--no-llm", action="store_true", help="글 생성을 건너뛰고 프롬프트 팩만")

    p_titles = sub.add_parser("titles", help="제목 후보 보기 / 고른 것과 조회수 기록")
    t_sub = p_titles.add_subparsers(dest="titles_cmd", required=True)
    t_show = t_sub.add_parser("show", help="그날 후보 보기")
    t_show.add_argument("--date", help="날짜 (기본: 오늘)")
    t_log = t_sub.add_parser("log", help="고른 제목과 조회수 기록")
    t_log.add_argument("--date", required=True)
    t_log.add_argument("--kind", required=True, choices=["blog", "longform", "shorts"])
    t_log.add_argument("--pick", required=True, type=int, help="후보 번호 (1부터)")
    t_log.add_argument("--views", type=int, help="조회수")
    t_log.add_argument("--title", help="후보에 없는 제목을 썼다면")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)

    if args.command == "run":
        return _cmd_run(cfg, args)
    if args.command == "collect":
        return _cmd_collect(cfg, args)
    if args.command == "render":
        return _cmd_render(cfg, args)
    if args.command == "site":
        return _cmd_site(cfg)
    if args.command == "doctor":
        return _cmd_doctor(cfg, verbose=args.verbose)
    if args.command == "notify":
        return _cmd_notify(cfg, args)
    if args.command == "weekly":
        return _cmd_weekly(cfg, args)
    if args.command == "titles":
        return _cmd_titles(cfg, args)
    return 1


# ── 명령별 처리 ──────────────────────────────────────────────


def _cmd_run(cfg, args) -> int:
    use_llm = False if args.no_llm else None
    result = run_pipeline(cfg, run_date=args.date, use_llm=use_llm, limit=args.limit)
    _report(result)
    _notify_result(cfg, result)
    return 0 if result.files else 1


def _cmd_collect(cfg, args) -> int:
    date_str = args.date or local_now(cfg).strftime("%Y-%m-%d")
    articles, feed_results = collect(cfg, now=datetime.now(timezone.utc))

    path = cfg.output_dir / date_str / "raw" / "articles.json"
    save_raw(
        path,
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

    ok = sum(1 for r in feed_results if r.ok)
    print(f"기사 {len(articles)}건 수집 · 피드 {ok}/{len(feed_results)}개 정상")
    print(f"저장 → {_rel(cfg, path)}")
    print(f"산출물을 만들려면: python -m rebrief render --date {date_str}")
    return 0


def _cmd_render(cfg, args) -> int:
    date_str = args.date or local_now(cfg).strftime("%Y-%m-%d")
    use_llm = False if args.no_llm else None
    try:
        result = rerender(cfg, date_str, use_llm=use_llm)
    except FileNotFoundError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        print("먼저 `python -m rebrief collect` 를 실행하세요.", file=sys.stderr)
        return 1
    _report(result)
    return 0


def _cmd_notify(cfg, args) -> int:
    """저장된 산출물을 읽어 알림을 보낸다. 워크플로의 실패 단계에서도 쓴다."""
    from .notify import build_failure_message, send_telegram, telegram_configured

    if not telegram_configured():
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 알림을 보내지 않습니다.")
        return 0
    date_str = args.date or local_now(cfg).strftime("%Y-%m-%d")
    site_url = str(cfg.get("site.url", "") or "")
    if args.failed:
        ok = send_telegram(build_failure_message(date=date_str, site_url=site_url, run_url=args.run_url))
    else:
        ok = send_telegram(_message_from_output(cfg, date_str))
    print("알림을 보냈습니다." if ok else "알림 전송에 실패했습니다.")
    return 0 if ok else 1


def _message_from_output(cfg, date_str: str) -> str:
    """output/<날짜>/ 의 파일만으로 알림 문구를 만든다 (파이프라인 결과 객체 없이)."""
    import json

    from .notify import build_run_message

    out = cfg.output_dir / date_str
    headline, issues = "", 0
    data = out / "data.json"
    if data.exists():
        try:
            payload = json.loads(data.read_text(encoding="utf-8"))
            headline = payload.get("headline", "")
            issues = len(payload.get("issues", []))
        except (json.JSONDecodeError, OSError):
            pass
    return build_run_message(
        date=date_str, headline=headline, issues=issues, articles=0,
        site_url=str(cfg.get("site.url", "") or ""), warnings=[],
        llm_used=data.exists(), images=len(list(out.glob("img-*.png"))),
    )


def _notify_result(cfg, result) -> None:
    """토큰이 설정돼 있을 때만 실행 결과를 보낸다. 없으면 아무 말 없이 지나간다."""
    from .notify import build_run_message, send_telegram, telegram_configured

    if not telegram_configured():
        return
    headline = ""
    data = result.out_dir / "data.json"
    if data.exists():
        import json
        try:
            headline = json.loads(data.read_text(encoding="utf-8")).get("headline", "")
        except (json.JSONDecodeError, OSError):
            pass
    text = build_run_message(
        date=result.date, headline=headline, issues=result.issues, articles=result.articles,
        site_url=str(cfg.get("site.url", "") or ""), warnings=result.warnings,
        llm_used=result.llm_used, images=len(list(result.out_dir.glob("img-*.png"))),
    )
    print("📨 텔레그램 알림 " + ("전송" if send_telegram(text) else "실패"))


def _cmd_weekly(cfg, args) -> int:
    from .weekly import run_weekly

    result = run_weekly(cfg, end_date=args.end, use_llm=False if args.no_llm else None)
    print(f"\n🗓  {result.week}  ({result.start} ~ {result.end})  ·  브리핑 {result.days}일치")
    if result.files:
        print(f"\n생성된 파일 ({len(result.files)}개)")
        for path in result.files:
            print(f"  · {path}")
    if result.usage and result.usage.calls:
        print(f"\n💰 {result.usage.summary()}")
    if result.warnings:
        print("\n⚠️  확인이 필요한 사항")
        for w in result.warnings:
            print(f"  · {w}")
    print()
    return 0 if result.files else 1


def _cmd_titles(cfg, args) -> int:
    from .render import update_index
    from .store import TitleLog

    log_ = TitleLog(cfg.state_dir / "titles.json")
    if args.titles_cmd == "show":
        date_str = args.date or local_now(cfg).strftime("%Y-%m-%d")
        day = log_.days.get(date_str)
        if not day:
            print(f"{date_str} 에 기록된 제목 후보가 없습니다.")
            return 1
        for kind, entry in day.items():
            mark = f"  → 고름: {entry['pick']}번" + (f", 조회수 {entry['views']:,}" if entry.get("views") is not None else "") if entry.get("pick") else ""
            print(f"\n[{kind}]{mark}")
            for i, t in enumerate(entry.get("candidates", []), start=1):
                print(f"  {i}. {t}")
        print()
        return 0
    try:
        entry = log_.log_pick(args.date, args.kind, args.pick, args.views, args.title)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    log_.save()
    update_index(cfg)
    print(f"기록했습니다 — {args.date} {args.kind}: \"{entry['title']}\" ({entry['type']})"
          + (f", 조회수 {entry['views']:,}" if entry.get("views") is not None else ""))
    return 0


def _cmd_site(cfg) -> int:
    from .site import build_site

    dest = build_site(cfg)
    days = sum(1 for p in dest.iterdir() if p.is_dir() and p.name != "latest")
    print(f"사이트를 만들었습니다 — {dest}  (날짜 {days}일치)")
    print(f"브라우저로 열어 보기: {dest / 'index.html'}")
    return 0


def _cmd_doctor(cfg, verbose: bool = False) -> int:
    feeds = cfg.feeds
    enabled = [f for f in feeds if f.enabled]
    print(f"피드 {len(enabled)}개 점검 중… (전체 {len(feeds)}개, 꺼진 것 {len(feeds) - len(enabled)}개)\n")

    # 실패 사유는 아래 표에 다시 나오므로 수집기 경고 로그는 잠시 접어 둔다.
    if not verbose:
        logging.getLogger("rebrief.collect").setLevel(logging.ERROR)

    results = fetch_feeds(cfg, enabled)
    width = max((len(r.feed.id) for r in results), default=10)

    dead: list[str] = []
    for result in results:
        if result.ok:
            print(f"  ✅  {result.feed.id.ljust(width)}  {len(result.articles):>3}건   {result.feed.name}")
        else:
            dead.append(result.feed.id)
            status = f"HTTP {result.status}" if result.status else "연결 실패"
            print(f"  ❌  {result.feed.id.ljust(width)}  {status:>9}   {_short(result.error)}")

    print()
    healthy = len(results) - len(dead)
    print(f"정상 {healthy}개 / 실패 {len(dead)}개")

    if healthy == 0:
        # 전부 실패했다면 피드가 죽은 게 아니라 이쪽 네트워크 문제일 가능성이 크다.
        # 여기서 "피드를 끄세요"라고 안내하면 멀쩡한 소스를 전부 꺼버리게 된다.
        print(
            "\n피드가 하나도 응답하지 않았습니다. 개별 피드 문제라기보다는"
            "\n네트워크·프록시·방화벽 쪽을 먼저 확인하세요. sources.yaml 은 그대로 두시고요.",
            file=sys.stderr,
        )
        return 1

    if dead:
        print("\n계속 실패하는 피드는 config/sources.yaml 에서 다음처럼 꺼두세요:")
        for feed_id in dead:
            print(f"  - id: {feed_id}  →  enabled: false")
        print("\n(일시적 문제일 수 있으니 한 번 더 돌려보고 판단하세요.)")

    return 0


# ── 출력 ─────────────────────────────────────────────────────


def _report(result) -> None:
    print(f"\n📅 {result.date}  ·  기사 {result.articles}건  ·  이슈 {result.issues}개")

    if result.files:
        print(f"\n생성된 파일 ({len(result.files)}개)")
        for path in result.files:
            print(f"  · {path}")

    if result.usage and result.usage.calls:
        print(f"\n💰 {result.usage.summary()}")
    elif not result.llm_used:
        print("\n요약·대본은 생성하지 않았습니다 (prompt-pack.md 참고).")

    if result.warnings:
        print("\n⚠️  확인이 필요한 사항")
        for warning in result.warnings:
            print(f"  · {warning}")
    print()


def _short(message: str | None, limit: int = 88) -> str:
    """긴 예외 메시지를 표에 들어갈 길이로 줄인다 (전문은 raw/articles.json 에 남는다)."""
    text = " ".join((message or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rel(cfg, path: Path) -> Path:
    try:
        return path.relative_to(cfg.repo_root)
    except ValueError:
        return path
