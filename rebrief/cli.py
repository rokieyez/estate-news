"""명령줄 인터페이스.

    python -m rebrief run          오늘치 전체 실행 (수집 → 요약 → 대본)
    python -m rebrief collect      수집만 하고 원본 저장
    python -m rebrief render       저장된 원본으로 산출물만 다시 생성
    python -m rebrief doctor       RSS 피드가 살아있는지 점검
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

    sub.add_parser("doctor", help="RSS 피드 상태 점검")

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
    if args.command == "doctor":
        return _cmd_doctor(cfg, verbose=args.verbose)
    return 1


# ── 명령별 처리 ──────────────────────────────────────────────


def _cmd_run(cfg, args) -> int:
    use_llm = False if args.no_llm else None
    result = run_pipeline(cfg, run_date=args.date, use_llm=use_llm, limit=args.limit)
    _report(result)
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
