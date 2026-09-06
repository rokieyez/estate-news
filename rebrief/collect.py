"""RSS 수집 → 정규화 → 시간/키워드 필터 → 본문 보강."""

from __future__ import annotations

import calendar
import importlib.util
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Iterable

import feedparser
import requests

from .config import Config, Feed
from .models import Article, _canonical_url

log = logging.getLogger(__name__)

# 구글뉴스 제목은 "본래 제목 - 매체명" 형태로 끝난다.
_PUBLISHER_SUFFIX = re.compile(r"\s+-\s+([^-]{2,20})$")
# 제목 앞 대괄호 태그: [단독], [속보] 등
_LEADING_TAG = re.compile(r"^\s*\[([^\]]{1,10})\]\s*")
_HTML_TAG = re.compile(r"<[^>]+>")


class FeedResult:
    """doctor 명령이 쓰는 피드별 수집 결과."""

    def __init__(self, feed: Feed):
        self.feed = feed
        self.articles: list[Article] = []
        self.error: str | None = None
        self.status: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def collect(cfg: Config, now: datetime | None = None) -> tuple[list[Article], list[FeedResult]]:
    """설정된 피드를 모두 읽어 필터링된 기사 목록을 돌려준다."""
    now = now or datetime.now(timezone.utc)
    results = fetch_feeds(cfg, cfg.enabled_feeds)

    articles: list[Article] = []
    for result in results:
        articles.extend(result.articles)

    articles = dedupe(articles)
    articles = filter_by_time(articles, now, int(cfg.get("run.lookback_hours", 28)))
    articles = filter_excluded(articles, cfg.exclude_terms)
    articles.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    limit = int(cfg.get("run.max_articles", 300))
    articles = articles[:limit]

    if cfg.get("collect.fetch_body", True):
        enrich_bodies(cfg, articles)

    return articles, results


# ── 피드 읽기 ────────────────────────────────────────────────


def fetch_feeds(cfg: Config, feeds: Iterable[Feed]) -> list[FeedResult]:
    feeds = list(feeds)
    timeout = int(cfg.get("collect.timeout_seconds", 15))
    workers = min(int(cfg.get("collect.max_workers", 8)), max(len(feeds), 1))
    headers = {"User-Agent": cfg.get("collect.user_agent", "rebrief/1.0")}

    results: list[FeedResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, f, headers, timeout): f for f in feeds}
        for future in as_completed(futures):
            results.append(future.result())

    order = {f.id: i for i, f in enumerate(feeds)}
    results.sort(key=lambda r: order.get(r.feed.id, 0))
    return results


def _fetch_one(feed: Feed, headers: dict[str, str], timeout: int) -> FeedResult:
    result = FeedResult(feed)
    try:
        response = requests.get(feed.url, headers=headers, timeout=timeout)
        result.status = response.status_code
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        result.articles = [
            a for a in (parse_entry(entry, feed) for entry in parsed.entries) if a is not None
        ]
        if not result.articles and not parsed.entries:
            result.error = "항목이 비어 있음 (피드 주소가 바뀌었을 수 있음)"
    except requests.RequestException as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # 피드 형식이 깨진 경우까지 여기서 흡수
        result.error = f"{type(exc).__name__}: {exc}"
    if result.error:
        log.warning("피드 실패 [%s] %s", feed.id, result.error)
    else:
        log.info("피드 수집 [%s] %d건", feed.id, len(result.articles))
    return result


def parse_entry(entry, feed: Feed) -> Article | None:
    raw_title = (getattr(entry, "title", "") or "").strip()
    url = (getattr(entry, "link", "") or "").strip()
    if not raw_title or not url:
        return None

    title, publisher = split_publisher(raw_title)
    title, tags = strip_leading_tags(title)

    return Article(
        id=Article.make_id(url, title),
        title=title,
        url=_canonical_url(url),
        feed_id=feed.id,
        feed_name=feed.name,
        publisher=publisher,
        published=parse_published(entry),
        summary=clean_html(getattr(entry, "summary", "") or ""),
        source_weight=feed.weight,
        tags=tags,
    )


def split_publisher(title: str) -> tuple[str, str]:
    """'제목 - 한국경제' → ('제목', '한국경제')."""
    match = _PUBLISHER_SUFFIX.search(title)
    if not match:
        return title, ""
    publisher = match.group(1).strip()
    # 매체명이 아니라 제목의 일부인 경우(공백 포함 긴 구절)는 자르지 않는다.
    if len(publisher.split()) > 3:
        return title, ""
    return title[: match.start()].strip(), publisher


def strip_leading_tags(title: str) -> tuple[str, list[str]]:
    """'[단독] 제목' → ('제목', ['단독']). 태그는 여러 개 붙어 있을 수 있다."""
    tags: list[str] = []
    while True:
        match = _LEADING_TAG.match(title)
        if not match:
            break
        tags.append(match.group(1).strip())
        title = title[match.end():]
    return title.strip(), tags


def parse_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        struct = getattr(entry, key, None)
        if struct:
            # feedparser 는 UTC 기준 struct_time 을 준다.
            return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)
    return None


def clean_html(text: str) -> str:
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", text)).strip()


# ── 필터 ─────────────────────────────────────────────────────


def dedupe(articles: list[Article]) -> list[Article]:
    """같은 기사(동일 URL 해시 또는 동일 제목)를 한 건으로 합친다."""
    by_id: dict[str, Article] = {}
    seen_titles: dict[str, str] = {}

    for article in articles:
        title_key = re.sub(r"[^0-9A-Za-z가-힣]", "", article.title)
        existing_id = seen_titles.get(title_key)
        if existing_id and existing_id in by_id:
            # 이미 같은 제목이 있으면 본문/요약이 더 긴 쪽을 남긴다.
            kept = by_id[existing_id]
            if len(article.best_text) > len(kept.best_text):
                by_id[existing_id] = article.model_copy(update={"id": kept.id})
            continue
        if article.id in by_id:
            continue
        by_id[article.id] = article
        seen_titles[title_key] = article.id

    return list(by_id.values())


def filter_by_time(articles: list[Article], now: datetime, lookback_hours: int) -> list[Article]:
    cutoff = now - timedelta(hours=lookback_hours)
    kept = []
    for article in articles:
        # 발행 시각을 모르는 기사는 버리지 않고 남긴다 (피드가 시각을 안 주는 경우가 있음).
        if article.published is None or article.published >= cutoff:
            kept.append(article)
    return kept


def filter_excluded(articles: list[Article], exclude_terms: list[str]) -> list[Article]:
    if not exclude_terms:
        return articles
    return [
        a for a in articles
        if not any(term in a.title for term in exclude_terms)
    ]


# ── 본문 보강 ────────────────────────────────────────────────


def enrich_bodies(cfg: Config, articles: list[Article]) -> None:
    """상위 기사 본문을 긁어 Article.body 를 채운다 (실패해도 그냥 넘어감)."""
    if importlib.util.find_spec("bs4") is None:
        log.info("beautifulsoup4 미설치 — 본문 수집을 건너뜁니다.")
        return

    top_n = int(cfg.get("collect.fetch_body_top_n", 20))
    timeout = int(cfg.get("collect.timeout_seconds", 15))
    max_chars = int(cfg.get("collect.body_max_chars", 4000))
    headers = {"User-Agent": cfg.get("collect.user_agent", "rebrief/1.0")}

    # 구글뉴스 링크는 리다이렉트 페이지라 본문이 없다.
    targets = [a for a in articles if "news.google.com" not in a.url][:top_n]
    if not targets:
        return

    workers = min(int(cfg.get("collect.max_workers", 8)), len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_body, a.url, headers, timeout, max_chars): a
            for a in targets
        }
        for future in as_completed(futures):
            article = futures[future]
            body = future.result()
            if body:
                article.body = body

    filled = sum(1 for a in targets if a.body)
    log.info("본문 수집 %d/%d건 성공", filled, len(targets))


def _fetch_body(url: str, headers: dict[str, str], timeout: int, max_chars: int) -> str:
    from bs4 import BeautifulSoup

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        soup = BeautifulSoup(response.text, "html.parser")
        for junk in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            junk.decompose()
        return extract_main_text(soup, max_chars)
    except Exception as exc:
        log.debug("본문 실패 %s: %s", url, exc)
        return ""


def extract_main_text(soup, max_chars: int) -> str:
    """<p> 가 가장 많이 모인 블록을 본문으로 본다."""
    best_text = ""
    candidates = soup.find_all(["article", "div", "section"])
    for node in candidates:
        paragraphs = node.find_all("p", recursive=False) or node.find_all("p")
        if len(paragraphs) < 3:
            continue
        text = " ".join(p.get_text(" ", strip=True) for p in paragraphs)
        if len(text) > len(best_text):
            best_text = text

    if not best_text:
        best_text = " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))

    text = re.sub(r"\s+", " ", best_text).strip()
    return text[:max_chars]
