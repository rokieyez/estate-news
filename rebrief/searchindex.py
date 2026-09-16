"""네이버 검색이 내 글을 잡았는지 매일 재는 자.

**왜 있는가.** 2026-09-16 에 「글이 검색에 안 나온다」를 손으로 재 보니 두 블로그가 서로 다른
상태였다 — 정치 쪽은 9편 중 7편이 색인돼 1~3위였고, 부동산 쪽은 10편 전부 누락이었다.
그런데 부동산 블로그는 그때 **총 10편에 첫 글이 열흘 전**이었다. 즉 「고장」이 아니라
「아직」일 수 있는데, 그걸 가르려면 시간이 지나는 동안 숫자가 쌓여야 한다.
기다리기로 정했으면(2026-09-16 로키즈 선택) **기다리는 동안 재는 것**이 있어야 2~4주 뒤에
감이 아니라 곡선을 보고 판단할 수 있다.

**어떻게 재는가.** 글 제목을 통째로 네이버 블로그탭에 넣고, 결과 HTML 에
`blog.naver.com/<아이디>/<글번호>` 가 있는지 본다. 제목 전체를 넣는 이유는 그것이
「색인됐는가」만 묻는 가장 깨끗한 질문이기 때문이다 — 짧은 키워드는 색인 여부가 아니라
순위 경쟁을 재게 된다.

**탐침이 고장 났을 때 누락으로 적지 않는다.** 네이버가 막거나 화면이 바뀌면 결과가 0건으로
보이는데, 그것을 「검색에서 빠졌다」로 적으면 장부가 거짓말을 한다. 그래서 잴 때마다
**블로그 글이 반드시 나오는 검색어(canary)를 먼저 한 번** 넣어 보고, 그것마저 0건이면
그날은 아무것도 적지 않는다.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

import requests

log = logging.getLogger(__name__)

RSS_URL = "https://rss.blog.naver.com/{blog_id}.xml"
LIST_URL = ("https://blog.naver.com/PostTitleListAsync.naver"
            "?blogId={blog_id}&currentPage=1&countPerPage={count}"
            "&viewdate=&categoryNo=0&parentCategoryNo=0")
SEARCH_URL = "https://m.search.naver.com/search.naver?ssc=tab.m_blog.all&query={q}"

# 블로그 글이 늘 나오는 검색어. 이것이 0건이면 막힌 것이지 내 글이 빠진 것이 아니다.
CANARY = "강남 맛집"

# 네이버에 예의를 지킨다. 하루 10여 번이면 충분히 가볍다.
PAUSE_SEC = 2.5


@dataclass
class Post:
    log_no: str
    date: str          # YYYY-MM-DD
    title: str


@dataclass
class Probe:
    """글 한 편의 검색 결과."""
    post: Post
    indexed: bool
    rank: int | None       # 블로그탭 1페이지에서 몇 번째인가 (없으면 None)
    page_blogs: int        # 그 화면에 있던 블로그 글 수 (0 이면 검색어 자체가 빈 결과)


def fetch(url: str, timeout: float = 20) -> str:
    """한 쪽 받아 오기. 실패하면 빈 문자열 — 부르는 쪽이 「못 쟀다」로 다룬다."""
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            log.warning("네이버 응답 %s: %s", resp.status_code, url[:80])
            return ""
        return resp.text
    except requests.RequestException as exc:
        log.warning("네이버 요청 실패(%s): %s", type(exc).__name__, url[:80])
        return ""


def blog_id(cfg) -> str:
    """설정에서 네이버 블로그 아이디를 꺼낸다.

    `blog.naver.blog_id` 를 먼저 보고, 없으면 붙여넣기 화면이 쓰는 `write_url`
    (`https://blog.naver.com/<아이디>/postwrite`) 에서 뽑는다 — 아이디를 두 군데
    적게 하면 한쪽만 고쳐져 어긋난다.
    """
    explicit = str(cfg.get("blog.naver.blog_id", "") or "").strip()
    if explicit:
        return explicit
    url = str(cfg.get("blog.naver.write_url", "") or "")
    m = re.search(r"blog\.naver\.com/([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else ""


def _unescape(text: str) -> str:
    return (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"'))


def recent_posts(bid: str, limit: int = 10, fetcher=fetch) -> list[Post]:
    """RSS 에서 최근 글을 읽는다. RSS 는 전체공개 글만 싣는다 — 그래서 여기 있는 글은
    「검색에 나올 자격이 있는 글」이고, 이것만 재면 공개 설정 때문에 헷갈릴 일이 없다."""
    xml = fetcher(RSS_URL.format(blog_id=bid))
    if not xml:
        return []
    out: list[Post] = []
    for chunk in re.findall(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", chunk, re.S)
        g = re.search(r"<guid>[^<]*?/(\d+)</guid>", chunk, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", chunk, re.S)
        if not (t and g and d):
            continue
        try:
            when = datetime.strptime(d.group(1).strip()[:16], "%a, %d %b %Y").date().isoformat()
        except ValueError:
            when = ""
        out.append(Post(g.group(1), when, _unescape(t.group(1)).strip()))
        if len(out) >= limit:
            break
    return out


def total_posts(bid: str, fetcher=fetch) -> int | None:
    """블로그 전체 글 수. 「고장인가 어린가」를 가르는 데 이게 결정적이다 —
    글이 열 편뿐인 블로그가 검색에 없는 것은 고장이 아니다."""
    raw = fetcher(LIST_URL.format(blog_id=bid, count=1))
    m = re.search(r'"totalCount"\s*:\s*"?(\d+)', raw or "")
    return int(m.group(1)) if m else None


def _blog_pairs(html: str) -> list[tuple[str, str]]:
    """검색 결과에 나온 (블로그아이디, 글번호) 를 나온 차례대로, 중복 없이."""
    return list(dict.fromkeys(re.findall(r"blog\.naver\.com/([A-Za-z0-9_\-]+)/(\d+)", html)))


def probe_one(bid: str, post: Post, fetcher=fetch) -> Probe:
    """글 제목을 통째로 넣어 내 글이 결과에 있는지 본다."""
    html = fetcher(SEARCH_URL.format(q=urllib.parse.quote(post.title)))
    pairs = _blog_pairs(html)
    rank = next((i + 1 for i, (b, p) in enumerate(pairs) if b == bid and p == post.log_no), None)
    return Probe(post, rank is not None, rank, len(pairs))


def canary_ok(fetcher=fetch) -> bool:
    """탐침이 살아 있는가. 블로그가 늘 나오는 검색어가 0건이면 막힌 것이다."""
    return len(_blog_pairs(fetcher(SEARCH_URL.format(q=urllib.parse.quote(CANARY))))) > 0


def check(cfg, limit: int = 10, fetcher=fetch, pause: float = PAUSE_SEC) -> dict:
    """오늘치 측정 한 번. 못 쟀으면 `ok: False` 를 돌려주고 장부는 건드리지 않는다."""
    bid = blog_id(cfg)
    if not bid:
        return {"ok": False, "reason": "설정에서 네이버 블로그 아이디를 찾지 못했습니다 "
                                       "(blog.naver.blog_id 또는 blog.naver.write_url)"}
    if not canary_ok(fetcher):
        return {"ok": False, "reason": "네이버 검색이 응답하지 않습니다 — 오늘은 적지 않습니다 "
                                       "(막혔거나 화면이 바뀐 것이지, 글이 빠진 것이 아닙니다)"}

    posts = recent_posts(bid, limit, fetcher)
    if not posts:
        return {"ok": False, "reason": f"RSS 에서 글을 읽지 못했습니다 (blog_id={bid})"}

    probes: list[Probe] = []
    for i, post in enumerate(posts):
        if i:
            time.sleep(pause)
        probes.append(probe_one(bid, post, fetcher))

    return {
        "ok": True,
        "blog_id": bid,
        "total_posts": total_posts(bid, fetcher),
        "checked": len(probes),
        "indexed": sum(1 for p in probes if p.indexed),
        "probes": probes,
    }


class IndexLog:
    """날짜별 색인 성적표. 한 줄이 하루다."""

    def __init__(self, path: Path):
        import json
        self.path = path
        self.days: dict[str, dict] = {}
        if path.exists():
            try:
                self.days = json.loads(path.read_text(encoding="utf-8")).get("days", {}) or {}
            except (json.JSONDecodeError, OSError):
                self.days = {}

    def record(self, run_date: str, result: dict) -> dict:
        entry = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "blog_id": result.get("blog_id", ""),
            "total_posts": result.get("total_posts"),
            "checked": result.get("checked", 0),
            "indexed": result.get("indexed", 0),
            "posts": [
                {"log_no": p.post.log_no, "date": p.post.date, "title": p.post.title,
                 "indexed": p.indexed, "rank": p.rank}
                for p in result.get("probes", [])
            ],
        }
        self.days[run_date] = entry
        return entry

    def save(self) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": datetime.now().isoformat(timespec="seconds"), "days": self.days}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")


def build_index_hint(days: dict, today: str) -> str:
    """아침 알림에 붙일 한 줄. 잰 적이 없으면 빈 문자열."""
    dates = sorted(d for d in days if d <= today)
    if not dates:
        return ""
    cur = days[dates[-1]]
    checked, indexed = cur.get("checked", 0), cur.get("indexed", 0)
    if not checked:
        return ""

    bits = f"🔎 네이버 색인 {indexed}/{checked}편"
    if len(dates) >= 2:
        before = days[dates[-2]].get("indexed", 0)
        diff = indexed - before
        if diff:
            bits += f" ({diff:+d})"
    if indexed == 0:
        total = cur.get("total_posts")
        many = f", 블로그 전체 {total}편" if total else ""
        bits += f" — 아직 검색에 안 잡힙니다{many}"
    return bits
