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
                      images: int = 0, stats: dict | None = None,
                      usd: float = 0.0, krw_per_usd: float = 1400,
                      quiet: bool = False, paste_url: str = "") -> str:
    lines = [f"📅 {date} 부돌보 브리핑"]
    if headline:
        lines.append(headline)
    lines.append(f"이슈 {issues}개 · 기사 {articles}건" + (f" · 그림 {images}장" if images else ""))
    if usd:
        lines.append(f"💳 오늘 비용 ${usd:.2f} (약 {round(usd * krw_per_usd):,}원)")
    lines += stats_lines(stats)
    if quiet:
        # 실패가 아니라 판단입니다. 물음표가 아니라 마침표로 알립니다.
        lines.append("😴 오늘은 쉬어 가는 날로 봤습니다 — 여러 매체가 함께 다룬 이야기가 없습니다")
    elif paste_url:
        # 붙여넣기 모드 — 실패가 아니라 사람 차례입니다
        lines.append(f"📋 붙여넣기 차례 — 이슈를 열어 상자를 복사하세요: {paste_url}")
    elif not llm_used:
        lines.append("⚠️ 요약·대본은 만들지 못했습니다 (prompt-pack.md 참고)")
    for w in warnings[:3]:
        if paste_url and w.startswith("붙여넣기 차례"):
            continue                      # 위에 이미 링크로 적었다
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
        # 신고가는 거래량과 달이 다르다 (최근 두 달 계약분) — 언제 계약인지 붙인다
        day = str(hot.get("date") or "")
        when = f", {int(day[5:7])}/{int(day[8:10])} 계약" if len(day) == 10 and day[4] == "-" else ""
        out.append(f"📈 신고가 {hot['district']} {hot['name']} "
                   f"{hot['amount'] / 100_000_000:.1f}억 ({hot['pct']:+.1f}%{when})")
    return out


def build_failure_message(*, date: str, site_url: str = "", run_url: str = "", streak: int = 1,
                          retry_ahead: bool = True) -> str:
    """실패 알림. `retry_ahead` 는 07:25 안전망이 **아직 남아 있는지** — 07:25 실행 자체가
    실패했거나 낮에 손으로 돌린 것이 실패했는데 "한 번 더 시도합니다" 라고 하면 사람이
    기다리기만 합니다 (2026-09-09 아침에 그랬습니다)."""
    lines = [f"❌ {date} 부돌보 브리핑 실패"]
    if streak >= 2:
        lines.append(f"🚨 {streak}일 연속 실패입니다. 일시적 장애가 아닐 수 있어요 — API 키·한도, 피드 상태를 확인하세요.")
        lines.append("Actions 탭 → 피드 점검 워크플로를 한 번 돌려 보세요.")
    elif retry_ahead:
        lines.append("깃허브 Actions 로그를 확인하세요. 07:25 안전망 실행이 한 번 더 시도합니다.")
    else:
        lines.append("깃허브 Actions 로그를 확인하세요. 오늘은 자동 재시도가 없습니다 — "
                     "원인을 고친 뒤 Actions 에서 date 를 지정해 손으로 돌리세요.")
    if run_url:
        lines.append(run_url)
    return "\n".join(lines)
