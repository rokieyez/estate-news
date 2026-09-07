"""정부 정책 원문 — 보도자료 본문과 첨부 문서(HWP·PDF)를 찾아 3줄로 줄인다.

출처는 대한민국 정책브리핑(korea.kr) 보도자료 목록 하나로 모은다. 부처별 사이트를 각각
긁으면 화면이 바뀔 때마다 깨지는데, 정책브리핑은 모든 부처 발표가 같은 서식으로 모이기 때문이다.
목록에 부처·날짜·요약이 이미 있어 상세 페이지는 걸러진 건만 연다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import requests

from .config import Config

log = logging.getLogger(__name__)

LIST_URL = "https://www.korea.kr/briefing/pressReleaseList.do"
VIEW_URL = "https://www.korea.kr/briefing/pressReleaseView.do?newsId={news_id}"
BASE = "https://www.korea.kr"

_ITEM = re.compile(r'href="/briefing/pressReleaseView\.do\?newsId=(\d+)')
_DATE = re.compile(r"(20\d{2})\.(\d{2})\.(\d{2})")
_FILE = re.compile(
    r"([^<>\n|]{4,120}\.(?:hwpx?|pdf|zip|xlsx?|docx?|pptx?))\s*<.{0,400}?"
    r'href="(/common/download\.do\?[^"]+)"',
    re.IGNORECASE | re.DOTALL,
)
# 본문 컨테이너는 view_cont 로 시작해 article_footer(공유·이전글 영역) 앞에서 끝난다.
class _EmptyMatch:
    @staticmethod
    def group(_index: int = 0) -> str:
        return ""


_EMPTY = _EmptyMatch()

_BODY_START = re.compile(r'<div[^>]*class="[^"]*view_cont[^"]*"[^>]*>', re.IGNORECASE)
_BODY_END = re.compile(r'class="[^"]*(?:article_footer|view_opt)[^"]*"', re.IGNORECASE)


@dataclass
class PolicyDoc:
    news_id: str
    title: str
    dept: str
    date: str
    url: str
    lead: str = ""
    summary: list[str] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    body: str = ""
    who: str = ""          # 이 발표가 특히 상관있는 사람 (모델이 채운다)
    schedule: list[dict] = field(default_factory=list)   # 앞으로의 일정 (시행일·입법예고 마감 등)
    follow_ups: list[dict] = field(default_factory=list)  # 같은 정책의 지난 발표

    @property
    def has_file(self) -> bool:
        return bool(self.files)


# ── 내부 도구 (테스트에서 갈아끼우기 쉽게 모듈 함수로 둔다) ──


def _get(url: str, **kw):
    return requests.get(url, **kw)


def _strip(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return " ".join(text.split())


def parse_list(html: str) -> list[PolicyDoc]:
    """목록 화면에서 제목·부처·날짜·요약을 뽑는다. 상세는 아직 열지 않는다."""
    docs: list[PolicyDoc] = []
    blocks = re.split(r'(?=<a href="/briefing/pressReleaseView)', html)[1:]
    for block in blocks:
        m = _ITEM.search(block)
        if not m:
            continue
        news_id = m.group(1)
        if any(d.news_id == news_id for d in docs):
            continue
        chunk = block[:4000]
        # 목록 한 칸의 구조: <strong>제목</strong> <span class="lead">요약</span>
        #                   <span class="source"><span>날짜</span><span>부처</span></span>
        title = _strip((re.search(r"<strong[^>]*>(.*?)</strong>", chunk, re.DOTALL) or _EMPTY).group(1))[:120]
        lead = _strip((re.search(r'<span class="lead">(.*?)</span>', chunk, re.DOTALL) or _EMPTY).group(1))[:400]
        source = _strip((re.search(r'<span class="source">(.*?)</span>\s*</span>', chunk, re.DOTALL) or _EMPTY).group(1))
        dm = _DATE.search(source or chunk)
        day = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else ""
        dept = ""
        if dm and source:
            dept = source[dm.end():].strip().split(" ")[0]
        if not title:
            continue
        if title:
            docs.append(PolicyDoc(news_id=news_id, title=title, dept=dept, date=day,
                                  url=VIEW_URL.format(news_id=news_id), lead=lead))
    return docs


def parse_detail(html: str) -> tuple[str, list[dict]]:
    """상세 화면에서 (본문 텍스트, 첨부파일 목록)."""
    body = ""
    start = _BODY_START.search(html)
    if start:
        rest = html[start.end():]
        end = _BODY_END.search(rest)
        body = _strip(rest[:end.start()] if end else rest[:20000])
        # 상세 화면의 본문 자리에는 대개 "이 자료는 …전재하여 제공" 안내만 있다. 진짜 본문은 첨부 PDF 안에 있다.
        if len(body) < 120 or "전재하여" in body:
            body = ""
    files: list[dict] = []
    seen: set[str] = set()
    for name, href in _FILE.findall(html):
        href = href.replace("&amp;", "&")
        clean = " ".join(name.split())
        if href in seen:
            continue
        seen.add(href)
        files.append({"name": clean, "url": BASE + href})
    return body, files


# 제목에 이 말이 있으면 어느 부처가 냈든 부동산 발표로 본다
STRONG = ("부동산", "주택", "아파트", "전세", "월세", "청약", "분양", "종부세", "종합부동산세",
          "재건축", "재개발", "임대차", "공시가격", "주거")


def _matches(doc: PolicyDoc, departments: list[str], keywords: list[str]) -> bool:
    """키워드는 반드시, 부처는 거들기만. 부처 이름 표기가 바뀌어도 놓치지 않게 한다."""
    haystack = f"{doc.title} {doc.lead}"
    if keywords and not any(k in haystack for k in keywords):
        return False
    if not departments:
        return True
    return any(d in doc.dept for d in departments) or any(k in doc.title for k in STRONG)


def fetch(cfg: Config, run_date: str, *, days: int = 1, limit: int = 3) -> list[PolicyDoc]:
    """오늘(또는 최근 며칠) 나온 부동산 관련 정부 발표. 실패하면 빈 목록."""
    policy = cfg.get("policy", {}) or {}
    if not policy.get("enabled", True):
        return []
    departments = list(policy.get("departments", []) or [])
    keywords = list(policy.get("keywords", []) or [])
    timeout = float(cfg.get("collect.timeout_seconds", 15))
    headers = {"User-Agent": str(cfg.get("collect.user_agent", "rebrief/1.0"))}

    try:
        end = date.fromisoformat(run_date)
    except ValueError:
        end = date.today()
    start = end - timedelta(days=max(days - 1, 0))
    params = {"startDate": start.isoformat(), "endDate": end.isoformat(), "pageIndex": "1"}

    # 한 화면에 20건뿐이라 며칠치를 보려면 여러 장을 넘겨야 한다
    pages = int(policy.get("list_pages", 4))
    collected: list[PolicyDoc] = []
    for index in range(1, max(pages, 1) + 1):
        try:
            resp = _get(LIST_URL, params={**params, "pageIndex": str(index)},
                        headers=headers, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("정책 원문 목록을 가져오지 못했습니다: %s", type(exc).__name__)
            break
        page_docs = parse_list(resp.text)
        if not page_docs:
            break
        collected += page_docs

    seen_ids: set[str] = set()
    docs = []
    for d in collected:
        if d.news_id in seen_ids or not _matches(d, departments, keywords):
            continue
        seen_ids.add(d.news_id)
        docs.append(d)
    docs = [d for d in docs if not d.date or start.isoformat() <= d.date <= end.isoformat()]
    docs = docs[:limit]

    for doc in docs:
        try:
            detail = _get(doc.url, headers=headers, timeout=timeout)
            detail.raise_for_status()
            doc.body, doc.files = parse_detail(detail.text)
        except requests.RequestException as exc:
            log.warning("정책 원문 상세를 열지 못했습니다(%s): %s", doc.news_id, type(exc).__name__)
    return docs


def check_source(cfg: Config) -> tuple[bool, int, int, str]:
    """정책브리핑 목록 한 장을 받아 (성공 여부, 전체 건수, 부동산 관련 건수, 오류) 를 준다."""
    policy = cfg.get("policy", {}) or {}
    departments = list(policy.get("departments", []) or [])
    keywords = list(policy.get("keywords", []) or [])
    try:
        resp = _get(
            LIST_URL,
            params={"pageIndex": "1"},
            headers={"User-Agent": str(cfg.get("collect.user_agent", "rebrief/1.0"))},
            timeout=float(cfg.get("collect.timeout_seconds", 15)),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        return False, 0, 0, type(exc).__name__
    docs = parse_list(resp.text)
    hits = sum(1 for d in docs if _matches(d, departments, keywords))
    return bool(docs), len(docs), hits, ""


def _clip(text: str, limit: int = 90) -> str:
    """길면 자르되 낱말 중간에서 끊지 않는다."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = max(cut.rfind(" "), cut.rfind(","), cut.rfind("·"))
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,·") + "…"


def bullets(lead: str) -> list[str]:
    """목록에 실린 부처의 요약 불릿. '…확산- 물리적…' 처럼 붙어 있어 한글 뒤의 '-' 로 끊는다."""
    parts = re.split(r"(?<=[가-힣\)\]])\s*-\s*", " ".join((lead or "").split()))
    return [p.strip(" -·") for p in parts if len(p.strip(" -·")) > 10]


_BOILERPLATE = ("관련 보도자료 내용입니다", "자세한 내용은 첨부파일", "이 자료는")
# 보도자료 PDF 는 머리말(보도시점·배포일) 뒤에 □·ㅇ 로 항목을 나눈다. 그 앞부분은 내용이 아니다.
_DOC_HEAD = re.compile(r"^.{0,400}?(?=[□◇])", re.DOTALL)
_DOC_BULLET = re.compile(r"[□◇ㅇ○•]\s*")


def pdf_text(path: Path, max_pages: int = 4) -> str:
    """내려받은 PDF 에서 글자를 뽑는다. 보도자료 본문은 korea.kr 화면이 아니라 이 파일에만 있다."""
    try:
        import pypdf
    except ImportError:                      # 없으면 조용히 건너뛴다
        return ""
    try:
        reader = pypdf.PdfReader(str(path))
        text = " ".join((page.extract_text() or "") for page in reader.pages[:max_pages])
    except Exception as exc:                 # 암호·손상 등 어떤 이유로든 실패할 수 있다
        log.info("PDF 에서 글자를 뽑지 못했습니다(%s): %s", path.name, type(exc).__name__)
        return ""
    return " ".join(text.split())


def extractive_summary(doc: PolicyDoc, lines: int = 3) -> list[str]:
    """LLM 없이 만드는 3줄. 부처가 목록에 적어 둔 요약 불릿을 그대로 쓴다.

    보도자료 본문은 문서 뷰어(iframe) 안에 있어 글자로 가져올 수 없다. 대신 목록의 요약은
    부처가 직접 쓴 것이라 지어내기 위험이 없다.
    """
    found = [b for b in bullets(doc.lead) if not any(x in b for x in _BOILERPLATE)]
    if found:
        return [_clip(b) for b in found[:lines]]
    if doc.body:
        chunks = doc_chunks(doc.body)
        if chunks:
            return [_clip(c) for c in chunks[:lines]]
    source = doc.lead or doc.title
    sentences = [s.strip() for s in re.split(r"(?<=[.!?다])\s+", source)
                 if len(s.strip()) > 15 and not any(x in s for x in _BOILERPLATE)]
    return [_clip(s) for s in sentences[:lines]] or [_clip(doc.title)]


def doc_chunks(body: str) -> list[str]:
    """보도자료 본문을 항목 단위로 쪼갠다. 머리말(보도시점·배포일)과 안내 문구는 뺀다."""
    text = " ".join((body or "").split())
    text = _DOC_HEAD.sub("", text, count=1)
    parts = [p.strip(" ·.-") for p in _DOC_BULLET.split(text)]
    out = []
    for part in parts:
        if len(part) < 20 or any(x in part for x in _BOILERPLATE):
            continue
        if part.startswith("<") and part.endswith(">"):
            continue
        out.append(re.sub(r"<[^>]{0,40}>", " ", part).strip())
    return [" ".join(o.split()) for o in out if o]


# ── 언제 시행되나 ────────────────────────────────────────────
#
# 보도자료에는 "10월 1일부터 시행", "9월 30일까지 입법예고" 같은 앞으로의 일정이 섞여 있습니다.
# 날짜만 뽑으면 통계표의 숫자까지 걸려들므로, 날짜 둘레에 일정을 뜻하는 낱말이 있을 때만 싣습니다.

_SCHED_WORDS = ("시행", "입법예고", "행정예고", "공포", "접수", "신청", "마감", "설명회",
                "공모", "발표", "적용", "실시", "개통", "지정", "공고", "착공", "준공")
_D_KOREAN = re.compile(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일")
_D_DOT = re.compile(r"['’](\d{2})\.\s?(\d{1,2})\.\s?(\d{1,2})\.")
_D_MONTH = re.compile(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*중")
_WINDOW = 70          # 날짜 앞뒤로 볼 글자 수


def _iso(year: int | None, month: int, day: int, base: date) -> str:
    """연도가 없으면 기준일로 미룬다. 이미 두 달 넘게 지난 달이면 내년으로 본다."""
    try:
        if year is None:
            guess = date(base.year, month, day)
            if (base - guess).days > 60:
                guess = date(base.year + 1, month, day)
            return guess.isoformat()
        return date(year, month, day).isoformat()
    except ValueError:                        # 2월 30일 같은 오탈자
        return ""


_BOUNDARY = "□ㅇ○◇\n"


def _clause(text: str, start: int, end: int) -> tuple[int, int]:
    """날짜가 들어 있는 한 문장의 시작·끝 위치. 부처 문서의 □·ㅇ 표시와 '~다.' 를 경계로 본다."""
    left = max((text.rfind(ch, 0, start) for ch in _BOUNDARY), default=-1)
    stop = text.rfind("다. ", 0, start)
    left = max(left, stop + 2 if stop >= 0 else -1)
    right_marks = [text.find(ch, end) for ch in _BOUNDARY]
    stop = text.find("다.", end)
    right_marks.append(stop + 2 if stop >= 0 else -1)
    rights = [r for r in right_marks if r >= 0]
    return left + 1, (min(rights) if rights else len(text))


def schedule_items(doc: PolicyDoc, run_date: str, limit: int = 4) -> list[dict]:
    """문서에서 앞으로의 일정만 뽑는다. 지나간 날짜와 통계표의 숫자는 뺀다."""
    try:
        base = date.fromisoformat(run_date)
    except ValueError:
        base = date.today()
    text = " ".join(((doc.body or "") + " " + (doc.lead or "")).split())
    hits: dict[tuple[str, str], dict] = {}

    for pattern in (_D_KOREAN, _D_DOT, _D_MONTH):
        for m in pattern.finditer(text):
            g = m.groups()
            if pattern is _D_MONTH:
                iso = _iso(int(g[0]) if g[0] else None, int(g[1]), 1, base)
            elif pattern is _D_DOT:
                iso = _iso(2000 + int(g[0]), int(g[1]), int(g[2]), base)
            else:
                iso = _iso(int(g[0]) if g[0] else None, int(g[1]), int(g[2]), base)
            if not iso or iso < base.isoformat():
                continue

            left, right = _clause(text, m.start(), m.end())
            clause = text[left:right].strip(" ·-")
            # 일정을 뜻하는 낱말은 날짜 뒤에 온다 ("10월 1일부터 시행"). 없으면 앞쪽도 본다.
            after, before = text[m.end():right], text[left:m.start()]
            word = (next((w for w in _SCHED_WORDS if w in after), "")
                    or next((w for w in _SCHED_WORDS if w in before), ""))
            if not word:
                continue
            # 같은 문장·같은 성격이면 한 줄로 묶고 늦은 날짜를 남긴다 (기간은 마감일이 중요하다)
            key = (clause[:28], word)
            row = {"date": iso, "kind": word, "text": _clip(clause, 90),
                   "title": doc.title, "url": doc.url}
            if key not in hits or iso > hits[key]["date"]:
                hits[key] = row

    rows = sorted(hits.values(), key=lambda r: (r["date"], r["kind"]))
    return rows[:limit]


def download(doc: PolicyDoc, dest: Path, cfg: Config) -> list[str]:
    """첨부 문서를 내려받아 파일명 목록을 돌려준다. 끄면 링크만 남는다."""
    policy = cfg.get("policy", {}) or {}
    if not policy.get("download", True) or not doc.files:
        return []
    max_mb = float(policy.get("max_file_mb", 20))
    max_files = int(policy.get("max_files_per_doc", 2))
    timeout = float(cfg.get("collect.timeout_seconds", 15))
    headers = {"User-Agent": str(cfg.get("collect.user_agent", "rebrief/1.0"))}
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    # 같은 문서가 hwp·hwpx·pdf 로 세 벌 올라오는 일이 많다. PDF 를 먼저, 확장자별로 하나씩.
    order = {".pdf": 0, ".hwpx": 1, ".hwp": 2}
    picked: list[dict] = []
    seen_ext: set[str] = set()
    for item in sorted(doc.files, key=lambda f: order.get(Path(f["name"]).suffix.lower(), 9)):
        ext = Path(item["name"]).suffix.lower()
        if ext in seen_ext:
            continue
        seen_ext.add(ext)
        picked.append(item)
        if len(picked) >= max_files:
            break
    for item in picked:
        # 마크다운 링크가 깨지지 않게 공백·괄호를 밑줄로 바꾼다
        name = re.sub(r"_+", "_", re.sub(r"[^\w가-힣.\-]", "_", item["name"])).strip("_")[:80]
        if not name:
            continue
        try:
            resp = _get(item["url"], headers=headers, timeout=timeout)
            resp.raise_for_status()
            if len(resp.content) > max_mb * 1024 * 1024:
                log.info("정책 첨부가 커서 건너뜁니다(%s)", name)
                continue
            (dest / name).write_bytes(resp.content)
            item["file"] = name
            saved.append(name)
            if name.lower().endswith(".pdf") and not doc.body:
                doc.body = pdf_text(dest / name)[:6000]
        except requests.RequestException as exc:
            log.warning("정책 첨부를 받지 못했습니다(%s): %s", name, type(exc).__name__)
    return saved
