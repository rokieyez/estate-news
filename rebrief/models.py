"""파이프라인 전 구간에서 쓰는 데이터 모델.

수집(Article) → 묶음(Cluster) → 분석(DailyBrief) → 콘텐츠(BlogPost/영상 대본)
순서로 흐릅니다. 뒤쪽 3개는 Claude 구조화 출력(structured outputs)의
스키마로 그대로 쓰이므로, 필드를 바꾸면 프롬프트도 같이 확인해야 합니다.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

# ── 수집 단계 ────────────────────────────────────────────────


class Article(BaseModel):
    """기사 1건."""

    id: str
    title: str
    url: str
    feed_id: str
    feed_name: str
    publisher: str = ""          # 구글뉴스 제목 끝의 "- 매체명"에서 추출
    published: Optional[datetime] = None
    summary: str = ""            # RSS 요약
    body: str = ""               # 본문 (수집 성공한 경우만)
    source_weight: float = 1.0
    tags: list[str] = Field(default_factory=list)   # [단독], [속보] 등

    @staticmethod
    def make_id(url: str, title: str) -> str:
        """URL 우선, 없으면 제목으로 안정적인 해시 ID를 만든다."""
        basis = _canonical_url(url) or title
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def text_for_matching(self) -> str:
        return f"{self.title} {self.summary}"

    @property
    def best_text(self) -> str:
        """요약에 넣을 본문. 본문이 있으면 본문, 없으면 RSS 요약."""
        return self.body or self.summary


def _canonical_url(url: str) -> str:
    """추적 파라미터를 떼어낸 URL. 같은 기사가 다른 링크로 들어오는 걸 막는다."""
    if not url:
        return ""
    url = re.sub(r"[?&](utm_[^&]+|fbclid|gclid|ref|from)=[^&]*", "", url)
    return url.rstrip("?&/").strip()


class Cluster(BaseModel):
    """같은 사건을 다룬 기사 묶음 = 하나의 '이슈' 후보."""

    key: str
    articles: list[Article]
    score: float = 0.0
    categories: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)

    @property
    def lead(self) -> Article:
        """대표 기사 — 본문이 있는 것 우선, 그다음 최신순."""
        return sorted(
            self.articles,
            key=lambda a: (bool(a.body), a.published or datetime.min),
            reverse=True,
        )[0]

    @property
    def size(self) -> int:
        return len(self.articles)

    @property
    def publishers(self) -> list[str]:
        seen: list[str] = []
        for a in self.articles:
            name = a.publisher or a.feed_name
            if name not in seen:
                seen.append(name)
        return seen


# ── 분석 단계 (Claude 구조화 출력) ──────────────────────────


class DataPoint(BaseModel):
    """영상 자막 카드·차트로 바로 옮길 수 있는 수치 1건."""

    label: str = Field(description="무엇에 대한 수치인지. 예: '서울 아파트 주간 매매가격 변동률'")
    value: str = Field(description="숫자 그대로. 예: '-0.03'")
    unit: str = Field(default="", description="단위. 예: '%', '만원', '건'")
    period: str = Field(default="", description="기준 시점. 예: '9월 첫째 주'")
    context: str = Field(default="", description="비교/추세 한 줄. 예: '3주 연속 하락, 낙폭은 축소'")
    source: str = Field(default="", description="출처 기관 또는 매체")


class IssueBrief(BaseModel):
    """오늘 다룰 이슈 1건의 정리 결과."""

    title: str = Field(description="이슈 제목. 15자 내외의 명사형")
    one_liner: str = Field(description="이 이슈를 한 문장으로. 40자 내외")
    category: str = Field(description="정책·규제 / 대출·금리 / 가격동향 / 공급·정비사업 / 청약·분양 / 전월세·임대 / 지역이슈 / 세금·절세 / 시장심리 중 하나")
    what_happened: list[str] = Field(description="확인된 사실만 3~5개. 각 항목은 한 문장")
    numbers: list[DataPoint] = Field(description="기사에 나온 수치. 없으면 빈 배열")
    why_it_matters: str = Field(description="시청자에게 어떤 의미인지 2~3문장")
    who_is_affected: list[str] = Field(description="영향받는 집단. 예: '수도권 무주택 실수요자'")
    caution: str = Field(description="확정이 아니거나 해석이 갈리는 지점. 없으면 '없음'")
    source_urls: list[str] = Field(description="근거 기사 URL")


class DailyBrief(BaseModel):
    """하루치 브리핑 전체."""

    date: str = Field(description="YYYY-MM-DD")
    headline: str = Field(description="오늘 부동산 시장을 한 줄로. 25자 내외")
    lead: str = Field(description="오늘의 흐름 요약 3~4문장")
    issues: list[IssueBrief]
    market_temperature: str = Field(description="시장 온도를 한 문장으로. 근거 수치를 포함")
    tomorrow_watch: list[str] = Field(description="내일·이번 주에 확인할 일정이나 지표 2~4개")


# ── 콘텐츠 단계 (Claude 구조화 출력) ────────────────────────


class ImageSlot(BaseModel):
    """본문의 [이미지: …] 자리 하나. 수치 그림 자리면 어떤 수치인지 라벨로 못박는다."""

    description: str = Field(description="어떤 이미지를 넣을지 설명. 본문의 [이미지: …] 안 문장과 같게")
    datapoint_label: str = Field(
        default="",
        description=(
            "이 자리가 브리핑의 수치(issues[].numbers[].label)를 그림으로 보여주는 자리라면 "
            "그 label 을 글자 그대로. 현장 사진·캡처 같은 자리면 빈 문자열"
        ),
    )
    search_keywords: str = Field(
        default="",
        description="사진 자리라면 스톡 사진 사이트에서 찾을 영어 검색어 2~4단어. 예: 'seoul apartment skyline'. 수치 자리면 빈 문자열",
    )


class BlogPost(BaseModel):
    title: str = Field(description="블로그 제목. 검색 유입을 고려하되 낚시성 금지")
    slug: str = Field(description="영문 소문자 하이픈 슬러그")
    meta_description: str = Field(description="검색결과 설명문. 80~120자")
    focus_keyword: str = Field(
        default="",
        description=(
            "이 글로 검색 유입을 노릴 대표 검색어 하나. 사람이 실제로 검색창에 칠 말로. "
            "2~4어절, 예: '종부세 대상 자치구'. 제목 앞쪽·첫 문단·소제목 한 곳 이상에 그대로 들어가야 한다"
        ),
    )
    summary_lines: list[str] = Field(
        default_factory=list,
        description="본문 맨 앞에 얹을 요약 3줄. 각 45자 내외. 검색으로 들어온 사람이 이것만 읽고도 알게",
    )
    takeaways: list[str] = Field(
        default_factory=list,
        description=(
            "'그래서 나는?' 2~3개. 읽는 사람 유형으로 시작해 무엇을 하면 되는지 한 줄. "
            "예: '무주택 실수요자라면, 지금은 대출 한도부터 확인할 때입니다'. 각 45자 내외"
        ),
    )
    closing_question: str = Field(
        default="",
        description="글 끝에 붙일 질문 한 문장. 댓글을 유도하되 구걸하지 않는 자연스러운 물음",
    )
    tags: list[str] = Field(description="태그 목록. 개수는 지시에 따름")
    body_markdown: str = Field(description="마크다운 본문. H2/H3 소제목, 표, 불릿 활용")
    image_slots: list[ImageSlot] = Field(
        default_factory=list,
        description="body_markdown 안의 [이미지: …] 자리와 같은 순서. 없으면 빈 배열",
    )


class CaptionLine(BaseModel):
    """쇼츠 자막 한 줄 = 화면에 한 번에 뜨는 단위."""

    at: str = Field(description="시작 타임코드. 예: '00:03'")
    text: str = Field(description="자막 문구. 한 줄 18자 이내로 끊을 것")
    visual: str = Field(description="이 구간에 깔 화면 지시. 예: '서울 아파트 단지 드론샷 + 하락률 자막 카드'")


class ShortsScript(BaseModel):
    title_candidates: list[str] = Field(description="쇼츠 제목 후보 3개")
    hook: str = Field(description="0~3초 훅 문장. 질문형 또는 수치 제시형")
    lines: list[CaptionLine] = Field(description="자막 단위로 쪼갠 대본 전체")
    cta: str = Field(description="마무리 유도 문장")
    hashtags: list[str] = Field(description="해시태그 5~8개. # 포함")
    estimated_seconds: int = Field(description="예상 길이(초)")


class LongformSection(BaseModel):
    chapter: str = Field(description="챕터 제목")
    at: str = Field(description="시작 타임코드. 예: '01:20'")
    script: str = Field(description="실제로 읽을 대본. 구어체, 문장 짧게")
    broll: list[str] = Field(description="이 구간에 필요한 자료화면·B롤 지시 2~4개")
    graphics: list[str] = Field(description="자막 카드/그래픽으로 띄울 수치나 문구")


class LongformScript(BaseModel):
    title_candidates: list[str] = Field(description="영상 제목 후보 5개")
    thumbnail_texts: list[str] = Field(description="썸네일에 넣을 짧은 문구 5개. 각 12자 이내")
    cold_open: str = Field(description="인트로 전 30초 후킹 멘트")
    sections: list[LongformSection]
    outro: str = Field(description="마무리 멘트 + CTA")
    description: str = Field(description="유튜브 설명란 전문. 챕터 타임코드와 출처 포함")
    tags: list[str] = Field(description="유튜브 태그 10~15개")
    pinned_comment: str = Field(description="고정 댓글로 쓸 요약 + 주의 문구")
    estimated_minutes: float = Field(description="예상 길이(분)")


class VideoPack(BaseModel):
    shorts: ShortsScript
    longform: LongformScript


# ── 주간 결산 (Claude 구조화 출력) ──────────────────────────


class WeeklyReview(BaseModel):
    """일주일치 브리핑을 묶은 결산 글."""

    title: str = Field(description="블로그 제목. '이번 주 부동산' 이 들어가면 좋다. 25~35자")
    slug: str = Field(description="영문 소문자 하이픈 슬러그")
    meta_description: str = Field(description="검색결과 설명문. 80~120자")
    five_lines: list[str] = Field(description="이번 주를 다섯 줄로. 각 줄 40자 이내, 숫자 포함")
    body_markdown: str = Field(description="마크다운 본문. 요일 순이 아니라 주제 순으로 묶는다")
    next_week_watch: list[str] = Field(description="다음 주에 볼 일정·지표 2~4개")
    tags: list[str] = Field(description="태그 목록")


class Rewrite(BaseModel):
    """금지 표현이 든 문장 하나를 고쳐 쓴 결과."""

    text: str = Field(description="같은 뜻을 유지하되 금지 표현을 뺀 문장. 길이는 원문과 비슷하게")

