"""실행 결과를 텔레그램으로 알린다.

봇 토큰과 채팅 ID 는 환경변수(또는 .env)에서 읽는다. 둘 중 하나라도 없으면
아무것도 하지 않고 조용히 넘어간다 — 알림은 있으면 좋은 것이지 필수가 아니다.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _post(url: str, **kw):
    return requests.post(url, **kw)


def telegram_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram(text: str, *, timeout: float = 15) -> bool:
    """메시지 한 건을 보낸다. 설정이 없거나 실패하면 False."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        resp = _post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=timeout,
        )
        if resp.status_code != 200:
            log.warning("텔레그램 전송 실패: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.RequestException as exc:
        # 예외 문자열에는 요청 URL(= 봇 토큰이 든 주소)이 섞이므로 종류만 남긴다
        log.warning("텔레그램 전송 실패: %s", type(exc).__name__)
        return False


def build_run_message(*, date: str, headline: str, issues: int, articles: int,
                      site_url: str, warnings: list[str], llm_used: bool,
                      images: int = 0, stats: dict | None = None) -> str:
    lines = [f"📅 {date} 부동산 브리핑"]
    if headline:
        lines.append(headline)
    lines.append(f"이슈 {issues}개 · 기사 {articles}건" + (f" · 그림 {images}장" if images else ""))
    lines += stats_lines(stats)
    if not llm_used:
        lines.append("⚠️ 요약·대본은 만들지 못했습니다 (prompt-pack.md 참고)")
    for w in warnings[:3]:
        lines.append(f"⚠️ {w}")
    if site_url:
        lines.append(f"🔗 {site_url.rstrip('/')}/latest/")
    return "\n".join(lines)


def stats_lines(stats: dict | None) -> list[str]:
    """실거래 요약 두 줄. 폰만 보고도 오늘 글을 올릴지 판단할 수 있게."""
    if not stats or not stats.get("districts"):
        return []
    diff = stats.get("total", 0) - stats.get("total_before", 0)
    out = [f"🏢 {stats.get('month_label', '')} 신고 매매 {stats.get('total', 0):,}건 ({diff:+,}건)"]
    hot = next((h for h in (stats.get("highlights") or []) if h["kind"] == "신고가"), None)
    if hot:
        out.append(f"📈 신고가 {hot['district']} {hot['name']} "
                   f"{hot['amount'] / 100_000_000:.1f}억 ({hot['pct']:+.1f}%)")
    return out


def build_failure_message(*, date: str, site_url: str = "", run_url: str = "", streak: int = 1) -> str:
    lines = [f"❌ {date} 부동산 브리핑 실패"]
    if streak >= 2:
        lines.append(f"🚨 {streak}일 연속 실패입니다. 일시적 장애가 아닐 수 있어요 — API 키·한도, 피드 상태를 확인하세요.")
        lines.append("Actions 탭 → 피드 점검 워크플로를 한 번 돌려 보세요.")
    else:
        lines.append("깃허브 Actions 로그를 확인하세요. 07:25 안전망 실행이 한 번 더 시도합니다.")
    if run_url:
        lines.append(run_url)
    return "\n".join(lines)
