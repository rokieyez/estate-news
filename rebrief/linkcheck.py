"""출처 링크가 살아있는지 확인한다.

발행 직전에 죽은 링크를 걸러내기 위한 것이다. 기사 본문을 읽지는 않고 HEAD 요청만
보내며, HEAD 를 막는 서버(405)는 GET 으로 한 번 더 두드린다.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from .config import Config

log = logging.getLogger(__name__)


# 테스트에서 갈아끼우기 쉽도록 얇게 감싼다. requests 전역을 건드리면 수집기 스텁과 충돌한다.
def _head(url: str, **kw):
    return requests.head(url, **kw)


def _get(url: str, **kw):
    return requests.get(url, **kw)


@dataclass
class LinkStatus:
    url: str
    ok: bool
    status: int | None = None
    error: str = ""

    @property
    def note(self) -> str:
        if self.ok:
            return ""
        return f"HTTP {self.status}" if self.status else (self.error or "연결 실패")


def check_links(cfg: Config, urls: list[str]) -> dict[str, LinkStatus]:
    """URL 목록을 병렬로 점검해 {url: LinkStatus} 로 돌려준다. 중복 URL 은 한 번만 본다."""
    unique = list(dict.fromkeys(u for u in urls if u))
    if not unique:
        return {}
    timeout = float(cfg.get("collect.timeout_seconds", 15))
    workers = int(cfg.get("collect.max_workers", 8))
    headers = {"User-Agent": str(cfg.get("collect.user_agent", "Mozilla/5.0 (compatible; rebrief)"))}

    def probe(url: str) -> LinkStatus:
        try:
            resp = _head(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code == 405 or resp.status_code >= 400 and resp.status_code != 404:
                # HEAD 를 거부하는 서버가 있다. 404 만 아니면 GET 으로 확인한다.
                resp = _get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
                resp.close()
            return LinkStatus(url, resp.status_code < 400, resp.status_code)
        except requests.RequestException as exc:
            return LinkStatus(url, False, None, type(exc).__name__)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(probe, unique))
    dead = [r for r in results if not r.ok]
    if dead:
        log.warning("출처 링크 %d/%d 개 확인 실패", len(dead), len(results))
    return {r.url: r for r in results}
