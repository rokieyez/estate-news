"""네이버에 올린 글이 검색에 걸리는지 확인한다.

발행 자체는 사람이 하고, 여기서는 **검색 결과에 나오는지만** 봅니다. 글을 올려도
검색에 안 잡히면 유입이 없으므로, 며칠 지나도 안 나오면 알려 주기 위한 장치입니다.

네이버 개발자센터에서 발급한 검색 API 키를 환경변수로 읽습니다
(`NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`). 키가 없으면 아무것도 하지 않고 넘어갑니다.
글쓰기 API 와 달리 검색 API 는 공개돼 있어 계정 위험이 없습니다.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://openapi.naver.com/v1/search/blog.json"
# blog.naver.com/아이디/글번호 · m.blog.naver.com/… · blog.naver.com/PostView.naver?…logNo=글번호
_POST_NO = re.compile(r"(?:/|logNo=)(\d{8,})")


def _get(url: str, **kw):
    return requests.get(url, **kw)


def configured() -> bool:
    return bool(os.environ.get("NAVER_CLIENT_ID")) and bool(os.environ.get("NAVER_CLIENT_SECRET"))


def post_id(url: str) -> str:
    """글 주소에서 글번호만 뽑는다. 모바일·PC 주소가 달라도 같은 글로 본다."""
    m = _POST_NO.search(str(url or ""))
    return m.group(1) if m else ""


def search_blog(query: str, display: int = 50, timeout: float = 15) -> list[dict]:
    """네이버 블로그 검색 결과. 실패하면 빈 목록."""
    if not configured() or not query.strip():
        return []
    try:
        resp = _get(
            SEARCH_URL,
            params={"query": query[:255], "display": max(1, min(display, 100)), "sort": "sim"},
            headers={"X-Naver-Client-Id": os.environ.get("NAVER_CLIENT_ID", ""),
                     "X-Naver-Client-Secret": os.environ.get("NAVER_CLIENT_SECRET", "")},
            timeout=timeout,
        )
        resp.raise_for_status()
        return list(resp.json().get("items", []) or [])
    except (requests.RequestException, ValueError) as exc:
        # 키가 노출되지 않도록 예외 메시지 대신 종류만 남긴다
        log.warning("네이버 검색 실패: %s", type(exc).__name__)
        return []


def check_post(title: str, url: str, display: int = 50) -> dict:
    """글 제목으로 검색해 내 글이 몇 번째에 나오는지 본다.

    돌려주는 값: {"indexed": bool, "rank": int|None, "checked": bool}
    `checked` 가 False 면 키가 없거나 검색이 실패한 것이지 '노출 안 됨' 이 아닙니다.
    """
    mine = post_id(url)
    items = search_blog(title, display=display)
    if not items:
        return {"indexed": False, "rank": None, "checked": False}
    for i, item in enumerate(items, start=1):
        link = str(item.get("link", "") or "")
        if mine and post_id(link) == mine:
            return {"indexed": True, "rank": i, "checked": True}
    return {"indexed": False, "rank": None, "checked": True}


def stale_days(published_at: str, today: str) -> int:
    """발행하고 며칠 지났는지. 날짜를 못 읽으면 0."""
    try:
        return (date.fromisoformat(today) - date.fromisoformat(published_at)).days
    except ValueError:
        return 0


def run(cfg, run_date: str, *, days_back: int = 7, warn_after: int = 3) -> dict:
    """최근 발행분을 훑어 검색 노출을 기록한다. 결과 요약을 돌려준다."""
    from .store import PublishLog, TitleLog

    book = PublishLog(cfg.state_dir / "published.json")
    titles = TitleLog(cfg.state_dir / "titles.json")
    if not configured():
        return {"checked": 0, "indexed": 0, "missing": [], "skipped": True}

    try:
        end = date.fromisoformat(run_date)
    except ValueError:
        end = date.today()

    checked = indexed = 0
    missing: list[dict] = []
    for i in range(days_back + 1):
        day = (end - timedelta(days=i)).isoformat()
        entry = book.get(day)
        url = str(entry.get("url", "") or "")
        if not url:
            continue
        title = titles.picked_title(day, "blog") or entry.get("title", "") or ""
        if not title:
            continue
        result = check_post(title, url)
        if not result["checked"]:
            continue
        checked += 1
        entry["indexed"] = result["indexed"]
        entry["indexed_rank"] = result["rank"]
        entry["indexed_checked_at"] = run_date
        if result["indexed"]:
            indexed += 1
        elif stale_days(day, run_date) >= warn_after:
            missing.append({"date": day, "title": title, "url": url,
                            "days": stale_days(day, run_date)})
    book.save()
    return {"checked": checked, "indexed": indexed, "missing": missing, "skipped": False}


def build_message(result: dict, site_url: str = "") -> str:
    """검색에 안 걸린 글이 있을 때만 보낼 문구."""
    rows = result.get("missing", [])
    if not rows:
        return ""
    lines = ["🔍 네이버 검색에 아직 안 걸린 글이 있습니다.", ""]
    for r in rows:
        lines.append(f"· {r['date']} ({r['days']}일째) {r['title'][:40]}")
        lines.append(f"  {r['url']}")
    lines += ["", "글이 지워졌거나 저품질로 분류됐을 수 있습니다.",
              "네이버에서 제목을 그대로 검색해 직접 확인해 보세요."]
    if site_url:
        lines.append(site_url)
    return "\n".join(lines)
