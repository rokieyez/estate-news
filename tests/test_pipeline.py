"""네트워크 없이 파이프라인 전 구간을 검증한다.

RSS 는 픽스처로, Claude 호출은 가짜 응답 객체로 대체한다.
템플릿은 StrictUndefined 라 변수 하나만 틀려도 여기서 바로 터진다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import yaml

from rebrief import pipeline
from rebrief.cluster import build_clusters, similarity
from rebrief.collect import split_publisher, strip_leading_tags
from rebrief.config import Config, load_config
from rebrief.models import (
    BlogPost,
    CaptionLine,
    DailyBrief,
    DataPoint,
    IssueBrief,
    LongformScript,
    LongformSection,
    ShortsScript,
    VideoPack,
)
from rebrief.rank import score_clusters, select_issues
from rebrief.render import RenderStats, Renderer, to_srt

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"
RUN_DATE = "2026-09-06"


# ── 픽스처 ───────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.text = content.decode("utf-8")
        self.status_code = status_code
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def feed_bytes() -> bytes:
    """발행 시각을 '방금'으로 채운 RSS 본문."""
    recent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
    return FIXTURE.read_text(encoding="utf-8").replace("__PUBDATE__", recent).encode("utf-8")


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """실제 config/ 를 읽되 출력·상태 경로만 임시 폴더로 돌린다."""
    real = load_config()
    settings = json.loads(json.dumps(real.settings, default=str))
    settings["output"]["dir"] = str(tmp_path / "output")
    settings["collect"]["fetch_body"] = False        # 본문 수집은 네트워크가 필요
    settings["run"]["skip_recent_days"] = 0

    # 피드는 픽스처 하나만 쓴다.
    sources = yaml.safe_load(yaml.safe_dump(real.sources, allow_unicode=True))
    sources["feeds"] = [
        {"id": "fixture", "name": "테스트피드", "url": "https://example.test/rss", "weight": 1.0}
    ]

    config = Config(settings=settings, sources=sources, config_dir=real.config_dir)
    config.repo_root = tmp_path
    return config


@pytest.fixture(autouse=True)
def stub_network(monkeypatch, feed_bytes):
    def fake_get(url, **kwargs):
        return FakeResponse(feed_bytes)

    monkeypatch.setattr("rebrief.collect.requests.get", fake_get)


# ── 수집 · 정규화 ────────────────────────────────────────────


def test_title_parsing():
    assert split_publisher("서울 아파트값 하락 - 한국경제") == ("서울 아파트값 하락", "한국경제")
    # 매체명처럼 안 생긴 긴 꼬리는 그대로 둔다
    title = "정부 대책 발표 - 아주 길고 긴 부제목 문장이 이어짐"
    assert split_publisher(title)[1] == ""
    assert strip_leading_tags("[단독][속보] 규제 완화") == ("규제 완화", ["단독", "속보"])


def test_similarity_groups_same_story():
    threshold = 0.35   # config/settings.yaml 기본값

    # 같은 사건을 다른 매체가 다르게 쓴 제목은 임계값을 넘어야 한다
    assert similarity("서울 아파트값 3주 연속 하락…낙폭은 축소",
                      "서울 아파트 매매가격 3주째 하락, 낙폭 줄어") > threshold
    assert similarity("강남 재건축 조합, 시공사 선정 난항",
                      "강남 재건축 시공사 선정 또 유찰") > threshold

    # 다른 사건은 넘지 않아야 한다
    assert similarity("서울 아파트값 하락", "국토부 청약 제도 개편") < threshold
    assert similarity("서울 아파트값 3주 연속 하락…낙폭은 축소",
                      "서울 아파트 청약 경쟁률 급등") < threshold


def test_collect_filters_and_dedupes(cfg):
    from rebrief.collect import collect

    articles, results = collect(cfg, now=datetime.now(timezone.utc))
    titles = [a.title for a in articles]

    assert len(results) == 1 and results[0].ok
    assert any("서울 아파트" in t for t in titles)
    # exclude_terms 로 걸러져야 하는 것들
    assert not any("운세" in t for t in titles)
    assert not any("부고" in t for t in titles)
    # [속보] 태그는 제목에서 떼고 tags 로 옮긴다
    breaking = [a for a in articles if "속보" in a.tags]
    assert breaking and "[속보]" not in breaking[0].title


def test_clustering_and_ranking(cfg):
    from rebrief.collect import collect

    articles, _ = collect(cfg, now=datetime.now(timezone.utc))
    clusters = score_clusters(cfg, build_clusters(cfg, articles))

    # 서울 아파트값 기사 3건이 한 이슈로 묶여야 한다
    biggest = max(clusters, key=lambda c: c.size)
    assert biggest.size >= 3
    assert "가격동향" in biggest.categories
    # 여러 매체가 다룬 이슈가 상위에 온다
    assert clusters[0].score >= clusters[-1].score

    issues = select_issues(cfg, clusters)
    assert 1 <= len(issues) <= int(cfg.get("run.max_issues"))


# ── 파이프라인 (LLM 없이) ────────────────────────────────────


def test_run_without_llm(cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = pipeline.run(cfg, run_date=RUN_DATE, use_llm=False)

    out = cfg.output_dir / RUN_DATE
    assert (out / "brief.md").exists()
    assert (out / "sources.md").exists()
    assert (out / "prompt-pack.md").exists()
    assert (out / "raw" / "articles.json").exists()
    assert (cfg.output_dir / "INDEX.md").exists()

    brief = (out / "brief.md").read_text(encoding="utf-8")
    assert "부동산 뉴스 스크랩" in brief
    assert "서울 아파트" in brief

    pack = (out / "prompt-pack.md").read_text(encoding="utf-8")
    assert "1단계" in pack and "3단계" in pack

    assert result.articles >= 6
    assert result.issues >= 1
    assert result.llm_used is False


def test_rerender_reuses_saved_raw(cfg):
    pipeline.run(cfg, run_date=RUN_DATE, use_llm=False)
    (cfg.output_dir / RUN_DATE / "brief.md").unlink()

    result = pipeline.rerender(cfg, RUN_DATE, use_llm=False)
    assert (cfg.output_dir / RUN_DATE / "brief.md").exists()
    assert result.articles >= 6


def test_seen_store_skips_repeats(cfg):
    cfg.settings["run"]["skip_recent_days"] = 3
    first = pipeline.run(cfg, run_date=RUN_DATE, use_llm=False)
    second = pipeline.run(cfg, run_date=RUN_DATE, use_llm=False)

    # 두 번째 실행은 같은 기사를 이미 다룬 것으로 보고 걸러야 한다.
    seen = json.loads((cfg.state_dir / "seen.json").read_text(encoding="utf-8"))
    assert seen["count"] >= first.articles
    assert second.articles == first.articles   # 수집량 자체는 같고
    # 걸러진 뒤 남은 게 없으면 재탕이라도 내보내므로 이슈는 여전히 생긴다
    assert second.issues >= 1


# ── 템플릿 (LLM 산출물) ──────────────────────────────────────


def make_brief() -> DailyBrief:
    return DailyBrief(
        date=RUN_DATE,
        headline="서울 아파트값 3주 연속 하락",
        lead="이번 주 서울 아파트 매매가격이 3주 연속 내렸습니다. 다만 낙폭은 줄었습니다.",
        market_temperature="하락세가 이어지지만 속도는 둔화되는 국면입니다.",
        issues=[
            IssueBrief(
                title="서울 아파트값 3주 연속 하락",
                one_liner="낙폭은 전주보다 축소됐습니다.",
                category="가격동향",
                what_happened=["서울 아파트 매매가격이 0.03% 내렸다.", "3주 연속 하락이다."],
                numbers=[
                    DataPoint(
                        label="서울 아파트 주간 매매가격 변동률",
                        value="-0.03",
                        unit="%",
                        period="9월 첫째 주",
                        context="전주 -0.05%에서 낙폭 축소",
                        source="한국부동산원",
                    )
                ],
                why_it_matters="매수 대기자에게는 관망 구간이 이어진다는 신호입니다.",
                who_is_affected=["서울 무주택 실수요자", "갈아타기 수요자"],
                caution="주간 통계라 표본이 제한적입니다.",
                source_urls=["https://example.test/news/1", "https://example.test/news/2"],
            ),
            IssueBrief(
                title="전세사기 지원 확대",
                one_liner="보증금 반환 지원이 넓어집니다.",
                category="전월세·임대",
                what_happened=["국토부가 지원 확대 방안을 발표했다."],
                numbers=[],
                why_it_matters="임차인 보호 범위가 넓어집니다.",
                who_is_affected=["전세 임차인"],
                caution="없음",
                source_urls=["https://example.test/news/4"],
            ),
        ],
        tomorrow_watch=["다음 주 주간 아파트 가격 동향 발표"],
    )


def make_pack() -> VideoPack:
    return VideoPack(
        shorts=ShortsScript(
            title_candidates=["서울 집값 3주째 하락", "낙폭은 왜 줄었나", "지금 사도 될까"],
            hook="서울 아파트값, 3주 연속 내렸습니다.",
            lines=[
                CaptionLine(at="00:00", text="서울 아파트값 3주째 하락", visual="단지 항공샷 + 자막 카드"),
                CaptionLine(at="00:04", text="이번 주 -0.03%", visual="'-0.03%' 큰 자막 카드"),
                CaptionLine(at="00:09", text="낙폭은 오히려 줄었습니다", visual="꺾은선 그래프"),
            ],
            cta="구독과 알림 설정 부탁드립니다.",
            hashtags=["#부동산", "#서울아파트", "#집값"],
            estimated_seconds=58,
        ),
        longform=LongformScript(
            title_candidates=["서울 집값 3주 연속 하락, 지금 시장 읽는 법"] * 5,
            thumbnail_texts=["3주 연속 하락", "-0.03%", "낙폭 축소", "관망 구간", "지금 사도?"],
            cold_open="오늘 서울 아파트값 이야기부터 하겠습니다.",
            sections=[
                LongformSection(
                    chapter="이번 주 숫자",
                    at="00:30",
                    script="한국부동산원 주간 통계부터 보겠습니다. 서울은 0.03% 내렸습니다.",
                    broll=["부동산원 통계 화면 캡처", "서울 아파트 단지 스톡"],
                    graphics=["-0.03% 자막 카드", "3주 추이 꺾은선"],
                ),
                LongformSection(
                    chapter="전세사기 대책",
                    at="03:10",
                    script="국토부가 전세사기 피해자 지원을 확대한다고 밝혔습니다.",
                    broll=["국토부 브리핑 자료화면"],
                    graphics=["지원 확대 항목 3줄 카드"],
                ),
            ],
            outro="오늘 정리는 여기까지입니다. 구독과 알림 설정 부탁드립니다.",
            description="00:00 오프닝\n00:30 이번 주 숫자\n03:10 전세사기 대책\n\n출처: 한국부동산원",
            tags=["부동산", "서울아파트", "집값", "전세사기"],
            pinned_comment="핵심 3줄 요약. 투자 판단의 책임은 본인에게 있습니다.",
            estimated_minutes=8.0,
        ),
    )


def make_post() -> BlogPost:
    return BlogPost(
        title="서울 아파트값 3주 연속 하락, 오늘의 부동산 브리핑",
        slug="seoul-apt-price-2026-09-06",
        meta_description="서울 아파트 매매가격이 3주 연속 하락했습니다. 낙폭 축소의 의미를 정리했습니다.",
        tags=["부동산", "서울아파트", "집값", "전세사기", "청약"],
        body_markdown="## 오늘의 시장\n\n서울 아파트값이 3주 연속 내렸습니다.\n\n| 항목 | 값 |\n| --- | --- |\n| 변동률 | -0.03% |\n",
    )


def test_all_templates_render(cfg, tmp_path):
    """LLM 경로의 템플릿 6종이 전부 렌더링되는지 확인한다."""
    from rebrief.collect import collect

    articles, _ = collect(cfg, now=datetime.now(timezone.utc))
    clusters = select_issues(cfg, score_clusters(cfg, build_clusters(cfg, articles)))

    out = tmp_path / "render"
    renderer = Renderer(cfg, out, RUN_DATE)
    brief, pack, post = make_brief(), make_pack(), make_post()
    stats = RenderStats(articles=len(articles), publishers=4, feeds_ok=1, feeds_total=1)

    renderer.brief(brief, stats)
    renderer.blog(post, clusters)
    renderer.shorts(pack)
    renderer.longform(pack)
    renderer.production_notes(brief, pack)
    renderer.sources(clusters, stats, [], [])
    renderer.data_json(brief)

    for name in (
        "brief.md", "blog.md", "script-shorts.md", "script-shorts.srt",
        "script-longform.md", "production-notes.md", "sources.md", "data.json",
    ):
        path = out / name
        assert path.exists(), f"{name} 이 생성되지 않았습니다"
        assert path.stat().st_size > 0, f"{name} 이 비어 있습니다"

    brief_md = (out / "brief.md").read_text(encoding="utf-8")
    assert "-0.03%" in brief_md            # 수치 표가 채워졌는지
    assert "확인 필요" in brief_md          # caution 블록
    assert "없음" not in brief_md.split("확인 필요")[1][:40]   # caution='없음' 은 숨김

    notes = (out / "production-notes.md").read_text(encoding="utf-8")
    assert "부동산원 통계 화면 캡처" in notes   # B-roll 이 체크리스트로 모였는지
    assert "총 3컷" in notes

    blog_md = (out / "blog.md").read_text(encoding="utf-8")
    assert blog_md.startswith("---")        # 프론트매터
    assert "seoul-apt-price" in blog_md

    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert data["datapoints"][0]["value"] == "-0.03"
    assert data["datapoints"][0]["issue"] == "서울 아파트값 3주 연속 하락"


def test_srt_is_monotonic_and_parsable():
    lines = [
        CaptionLine(at="00:00", text="첫 줄", visual="a"),
        CaptionLine(at="00:00", text="같은 시각", visual="b"),   # 겹치는 타임코드
        CaptionLine(at="깨진값", text="못 읽는 타임코드", visual="c"),
    ]
    srt = to_srt(lines, total_seconds=30)
    stamps = [ln for ln in srt.splitlines() if "-->" in ln]
    assert len(stamps) == 3

    starts = [s.split(" --> ")[0] for s in stamps]
    assert starts == sorted(starts), "자막 시작 시각이 단조 증가해야 합니다"
    assert srt.rstrip().endswith("못 읽는 타임코드")
