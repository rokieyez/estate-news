"""네트워크 없이 파이프라인 전 구간을 검증한다.

RSS 는 픽스처로, Claude 호출은 가짜 응답 객체로 대체한다.
템플릿은 StrictUndefined 라 변수 하나만 틀려도 여기서 바로 터진다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


from rebrief import pipeline
from rebrief.cluster import build_clusters, similarity
from rebrief.collect import split_publisher, strip_leading_tags
from rebrief.models import (
    BlogPost,
    CaptionLine,
    DailyBrief,
    DataPoint,
    ImageSlot,
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


# (공용 픽스처 FakeResponse / feed_bytes / cfg / stub_network 는 conftest.py 에 있다)


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
        title="서울 아파트값 3주 연속 하락, 9월 6일 부동산 브리핑",
        slug="seoul-apt-price-2026-09-06",
        meta_description="서울 아파트 매매가격이 3주 연속 하락했습니다. 낙폭 축소의 의미를 정리했습니다.",
        focus_keyword="서울 아파트값",
        summary_lines=["서울 아파트값이 3주 연속 내렸습니다.", "낙폭은 -0.03%로 줄었습니다.", "관망세가 이어집니다."],
        closing_question="여러분 동네 시세는 어떤가요?",
        tags=["부동산", "서울아파트", "집값", " 전세사기 ", "#청약", "부동산"],
        body_markdown=(
            "서울 아파트값이 3주 연속 내렸습니다.\n\n"
            "## 서울 아파트값 이번 주 숫자\n\n"
            "낙폭은 오히려 줄었습니다.\n\n"
            "| 항목 | 값 |\n| --- | --- |\n| 변동률 | -0.03% |\n\n"
            "[이미지: 한국부동산원 주간 통계 화면 캡처]\n\n"
            "## 오늘의 체크포인트\n\n"
            "- 하락폭 축소\n- 관망 지속\n"
        ),
        image_slots=[ImageSlot(description="한국부동산원 주간 통계 화면 캡처",
                               datapoint_label="서울 아파트 주간 매매가격 변동률")],
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

    renderer.blog_naver(post)

    for name in (
        "brief.md", "blog.md", "blog-naver.html", "script-shorts.md", "script-shorts.srt",
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


# ── 네이버 블로그 산출물 ────────────────────────────────────


def test_naver_html_converts_markdown_semantically():
    """스마트에디터는 마크다운을 모른다. 의미 태그로 변환돼 있어야 서식이 살아난다."""
    from rebrief.render import to_naver_html

    html = to_naver_html(make_post().body_markdown)

    assert "<h2>서울 아파트값 이번 주 숫자</h2>" in html
    assert "<table>" in html and "<th>항목</th>" in html
    assert "<ul>" in html and "<li>하락폭 축소</li>" in html
    # 마크다운 기호가 그대로 남으면 네이버 본문에 텍스트로 보인다
    assert "##" not in html
    assert "| ---" not in html


def test_naver_image_slot_becomes_placeholder():
    from rebrief.render import to_naver_html

    html = to_naver_html("[이미지: 통계 화면 캡처]")
    assert 'class="imgslot"' in html
    assert "통계 화면 캡처" in html
    assert "[이미지:" not in html


def test_naver_hashtags_are_normalized():
    from rebrief.render import format_hashtags

    tags = format_hashtags(["부동산", " 전세사기 ", "#청약", "부동산", "서울 아파트"])
    # 중복 제거, # 중복 방지, 공백 제거
    assert tags == "#부동산 #전세사기 #청약 #서울아파트"
    # 네이버 태그 상한 30개
    assert len(format_hashtags([f"태그{i}" for i in range(40)]).split()) == 30


def test_naver_html_copy_area_excludes_guide(cfg, tmp_path):
    """복사 버튼이 본문(#post)만 집는지 — 안내문이 붙여넣기에 섞이면 안 된다."""
    renderer = Renderer(cfg, tmp_path / "naver", RUN_DATE)
    path = renderer.blog_naver(make_post())
    html = path.read_text(encoding="utf-8")

    assert 'id="post"' in html and 'id="title"' in html and 'id="tags"' in html
    # 안내 문구는 복사 대상 영역 바깥(#post 종료 이후)에 있어야 한다
    guide_at = html.index("쓰는 법")
    post_close = html.index('<div class="hint">')
    assert guide_at > post_close
    assert "#부동산" in html


# ── LLM 경로 통합 (가짜 생성기) ──────────────────────────────


class FakeGenerator:
    """Claude 호출을 대신하는 가짜. 파이프라인 배선만 검증한다."""

    def __init__(self, cfg, model=None):
        from rebrief.llm import Usage

        self.cfg = cfg
        self.model = model
        self.usage = Usage(model="fake-model")
        self.calls: list[str] = []

    def generate_brief(self, clusters, run_date):
        self.calls.append("brief")
        self.usage.calls += 1
        return make_brief()

    def generate_blog(self, brief):
        self.calls.append("blog")
        return make_post()

    def generate_video(self, brief, stats=None):
        self.calls.append("video")
        return make_pack()

    def generate_weekly(self, days, week_label):
        self.calls.append("weekly")
        self.usage.calls += 1
        from rebrief.models import WeeklyReview
        return WeeklyReview(
            title=f"이번 주 부동산 다섯 줄, {week_label}",
            slug=f"weekly-{week_label.lower()}",
            meta_description="일주일치 부동산 뉴스를 다섯 줄과 주제별로 정리했습니다.",
            five_lines=[f"{d['date']} {d.get('headline', '')}" for d in days][:5],
            body_markdown="## 가격\n\n서울 아파트값이 3주 연속 내렸습니다.\n\n## 다음 주 볼 것\n\n- 주간 통계",
            next_week_watch=["한국부동산원 주간 통계"],
            tags=["부동산", "주간결산"],
        )


def test_llm_path_writes_every_artifact(cfg, monkeypatch):
    """요약이 켜진 실행에서 네이버 HTML 까지 전부 나오는지."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)

    result = pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)
    out = cfg.output_dir / RUN_DATE

    assert result.llm_used is True
    assert not result.warnings, result.warnings
    for name in (
        "brief.md", "blog.md", "blog-naver.html", "script-shorts.md",
        "script-shorts.srt", "script-longform.md", "production-notes.md",
        "sources.md", "data.json",
    ):
        assert (out / name).exists(), f"{name} 이 생성되지 않았습니다"

    # 키가 있을 때는 프롬프트 팩을 만들지 않는다
    assert not (out / "prompt-pack.md").exists()

    # INDEX 에 네이버 열이 링크로 들어갔는지
    index = (cfg.output_dir / "INDEX.md").read_text(encoding="utf-8")
    assert "네이버" in index
    assert f"{RUN_DATE}/blog-naver.html" in index


def test_platform_markdown_skips_naver_html(cfg, monkeypatch):
    """platform: markdown 이면 네이버 HTML 은 만들지 않는다."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    cfg.settings["blog"]["platform"] = "markdown"

    pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)
    out = cfg.output_dir / RUN_DATE

    assert (out / "blog.md").exists()
    assert not (out / "blog-naver.html").exists()


def test_blog_failure_does_not_stop_video(cfg, monkeypatch):
    """블로그 생성이 실패해도 영상 대본은 나와야 한다."""
    from rebrief.llm import LLMError

    class BlogFails(FakeGenerator):
        def generate_blog(self, brief):
            raise LLMError("일시적 오류")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", BlogFails)

    result = pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)
    out = cfg.output_dir / RUN_DATE

    assert (out / "brief.md").exists()
    assert not (out / "blog.md").exists()
    assert (out / "script-shorts.md").exists()
    assert (out / "production-notes.md").exists()
    assert any("블로그 생성 실패" in w for w in result.warnings)


# ── 휴대폰용 사이트 ──────────────────────────────────────────


def test_site_build(cfg, monkeypatch, tmp_path):
    """실행 결과가 링크 하나로 열리는 사이트가 되는지."""
    from rebrief.site import build_site

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    pipeline.run(cfg, run_date="2026-09-05", use_llm=True)
    pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)

    site = build_site(cfg, dest=tmp_path / "site")

    # 마크다운은 브라우저에서 읽히도록 HTML 로 바뀌어야 한다
    assert (site / RUN_DATE / "brief.html").exists()
    assert not (site / RUN_DATE / "brief.md").exists()
    # 네이버 페이지는 복사 버튼이 있으므로 그대로 옮긴다
    assert (site / RUN_DATE / "blog-naver.html").exists()
    # 자막·데이터 파일도 내려받을 수 있어야 한다
    assert (site / RUN_DATE / "script-shorts.srt").exists()

    # latest/ 는 항상 최신 날짜의 사본 — 주소가 바뀌지 않아야 즐겨찾기가 유효하다
    assert (site / "latest" / "blog-naver.html").exists()
    assert (site / "latest" / "blog-naver.html").read_text(encoding="utf-8") == \
           (site / RUN_DATE / "blog-naver.html").read_text(encoding="utf-8")

    index = (site / "index.html").read_text(encoding="utf-8")
    assert RUN_DATE in index
    assert "latest/blog-naver.html" in index      # 첫 번째 버튼이 네이버 글
    assert "2026-09-05" in index                  # 지난 날짜 목록
    assert (site / ".nojekyll").exists()


def test_site_handles_empty_output(cfg, tmp_path):
    """아직 아무것도 안 만들었을 때도 안내 화면이 떠야 한다."""
    from rebrief.site import build_site

    site = build_site(cfg, dest=tmp_path / "site")
    index = (site / "index.html").read_text(encoding="utf-8")
    assert "아직 만들어진 글이 없습니다" in index
    assert not (site / "latest").exists()


def test_site_markdown_checkboxes_render():
    from rebrief.site import md_to_html

    html = md_to_html("- [ ] 촬영\n- [x] 대본 확인\n")
    assert "☐ 촬영" in html
    assert "☑ 대본 확인" in html
    assert "[ ]" not in html


# ── 스팸 차단 · 보도자료 물량공세 방지 ──────────────────────
#
# 아래 값들은 2026-09-06 첫 실제 실행에서 수집한 214건을 근거로 한다.
# 구글뉴스가 부동산 키워드에 도박·코인 SEO 스팸을 12% 섞어 보냈고,
# 21개 매체가 받아쓴 보도자료가 6개 매체의 실제 이슈를 이겼다.


def _article(title: str, summary: str = "", size_id: str = "x"):
    from rebrief.models import Article

    return Article(
        id=size_id, title=title, url=f"https://e.test/{size_id}",
        feed_id="f", feed_name="테스트", summary=summary,
    )


def test_gambling_spam_is_filtered(cfg):
    from rebrief.collect import filter_excluded

    spam = [
        _article("용호 토토 학생를 위한 글쓰기 기술 심층 분석", size_id="s1"),
        _article("파라오 카지노 보증 : 실제 경험자들의 조언", size_id="s2"),
        _article("팬텀 블랙잭 해방 : 위험 피하기와 보호 조치", size_id="s3"),
        _article("오늘 경마 동영상 비교 팀 협업 체계적 방법", size_id="s4"),
        _article("바이 비트 24 시간 : 현황, 경향 및 미래 전망", size_id="s5"),
    ]
    real = [
        _article("서울 아파트값 3주 연속 하락", size_id="r1"),
        _article("국토부, 전세사기 피해 지원 확대", size_id="r2"),
    ]

    kept = filter_excluded(spam + real, cfg.exclude_terms, cfg.exclude_patterns)
    assert {a.id for a in kept} == {"r1", "r2"}


def test_pattern_avoids_substring_collision(cfg):
    """'포커'는 막되 기사 말머리 '[MD포커스]'는 살려야 한다."""
    from rebrief.collect import filter_excluded

    articles = [
        _article("올스타 포커 연구원를 위한 비판적 사고 해결책", size_id="spam"),
        _article("사옥 팔고 유휴부동산 내놓고…우리금융이 CET1에 매달리는 이유",
                 summary="[MD포커스] 사옥 팔고 유휴부동산 내놓고", size_id="real"),
    ]
    kept = filter_excluded(articles, cfg.exclude_terms, cfg.exclude_patterns)
    assert [a.id for a in kept] == ["real"]


def test_press_release_churn_does_not_outrank_real_news(cfg):
    """보도자료를 21곳이 받아써도, 실속 있는 이슈를 이기면 안 된다."""
    from rebrief.models import Cluster

    # 정책·규제 + 가격동향에 걸리는 실제 이슈. 6개 매체 보도.
    substantive = Cluster(
        key="s",
        articles=[
            _article("집값 안 꺾이면 2030년 서울 대부분 종부세 낸다",
                     summary="종합부동산세 과세 대상이 서울 22개구로 확대된다는 분석. "
                             "매매가격 상승이 이어질 경우다.",
                     size_id=f"s{i}")
            for i in range(6)
        ],
    )
    # 정비사업 키워드 하나에 걸리는 보도자료. 21개 매체가 그대로 받아씀.
    churn = Cluster(
        key="c",
        articles=[
            _article("롯데건설, 도곡우성 재건축 수주…올해 도시정비사업 4조 원 돌파",
                     size_id=f"c{i}")
            for i in range(21)
        ],
    )

    scored = score_clusters(cfg, [substantive, churn])
    assert scored[0].key == "s", (
        f"보도자료가 1위가 됐습니다 "
        f"(실속 {substantive.score:.2f} vs 보도자료 {churn.score:.2f})"
    )


def test_volume_cap_is_applied(cfg):
    """상한을 없애면 물량공세가 이긴다 — 상한이 실제로 작동하는지 확인."""
    from rebrief.models import Cluster

    def make(n: int, title: str, key: str) -> Cluster:
        return Cluster(key=key, articles=[_article(title, size_id=f"{key}{i}") for i in range(n)])

    big = make(40, "롯데건설 재건축 수주", "big")
    small = make(5, "롯데건설 재건축 수주", "small")

    cfg.settings["rank"]["volume_cap"] = 8
    score_clusters(cfg, [big, small])
    capped_gap = big.score - small.score

    cfg.settings["rank"]["volume_cap"] = 10_000
    score_clusters(cfg, [big, small])
    uncapped_gap = big.score - small.score

    assert capped_gap < uncapped_gap, "상한이 점수 차이를 줄이지 못했습니다"


# ── 인포그래픽 연결 ──────────────────────────────────────────


def test_llm_실행이_인포그래픽까지_만든다(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)

    out = cfg.output_dir / RUN_DATE
    svgs = sorted(out.glob("img-*.svg"))
    assert svgs, "이미지가 하나도 생성되지 않았습니다"
    # 표지가 맨 앞에 오고(검색 목록 썸네일), 자리 1번은 변동률 수치 카드다
    assert svgs[0].name == "img-0-cover.svg"
    card = next(p for p in svgs if p.name == "img-1-stat-card.svg")
    assert "-0.03" in card.read_text(encoding="utf-8")
    # png: false 로 껐으므로 PNG 는 없어야 한다 → 본문은 SVG 파일명을 가리킨다
    assert not list(out.glob("img-*.png"))
    blog = (out / "blog.md").read_text(encoding="utf-8")
    assert "![한국부동산원 주간 통계 화면 캡처](img-1-stat-card.svg)" in blog
    naver = (out / "blog-naver.html").read_text(encoding="utf-8")
    assert "imgslot has-file" in naver and 'class="preview nocopy" src="img-1-stat-card.svg"' in naver
    # 표지는 본문 맨 위, 3줄 요약보다 먼저 (첫 이미지가 썸네일이 되므로)
    assert naver.index("대표 이미지") < naver.index("3줄 요약") < naver.index("서울 아파트값이 3주")
    assert "이 글의 순서" not in naver          # 가짜 글은 소제목이 2개뿐 → 목차 없음
    assert "![대표 이미지](img-0-cover.svg)" in blog
    # 썸네일 두 장
    assert (out / "thumb-longform.svg").exists() and (out / "thumb-shorts.svg").exists()


def test_설정으로_인포그래픽을_끌_수_있다(cfg, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    cfg.settings.setdefault("images", {})["enabled"] = False
    pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)

    assert not list((cfg.output_dir / RUN_DATE).glob("img-*"))


# ── 주간 결산 ────────────────────────────────────────────────


def _seed_days(cfg, dates):
    for d in dates:
        day = cfg.output_dir / d
        day.mkdir(parents=True, exist_ok=True)
        (day / "data.json").write_text(json.dumps({
            "date": d, "headline": f"{d} 헤드라인", "market_temperature": "보합",
            "issues": [{"title": "이슈", "category": "가격동향", "one_liner": "한 줄", "numbers": []}],
            "datapoints": [],
        }, ensure_ascii=False), encoding="utf-8")


def test_주간_결산은_사흘_미만이면_만들지_않는다(cfg):
    from rebrief.weekly import run_weekly
    _seed_days(cfg, ["2026-09-05", "2026-09-06"])
    result = run_weekly(cfg, end_date="2026-09-06", use_llm=False)
    assert result.days == 2 and not result.files
    assert any("3일" in w for w in result.warnings)


def test_키가_없으면_프롬프트_팩만_남긴다(cfg, monkeypatch):
    from rebrief.weekly import run_weekly
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _seed_days(cfg, ["2026-09-01", "2026-09-03", "2026-09-06", "2026-08-20"])   # 8-20 은 범위 밖
    result = run_weekly(cfg, end_date="2026-09-06", use_llm=False)
    assert result.week == "2026-W36" and result.days == 3
    names = {p.name for p in result.files}
    assert names == {"data.json", "weekly-prompt-pack.md"}
    pack = (result.out_dir / "weekly-prompt-pack.md").read_text(encoding="utf-8")
    assert "2026-09-01 헤드라인" in pack and "2026-08-20" not in pack


def test_주간_결산_LLM_경로(cfg, monkeypatch):
    from rebrief.weekly import run_weekly
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.weekly.ContentGenerator", FakeGenerator)
    _seed_days(cfg, ["2026-09-02", "2026-09-04", "2026-09-06"])

    result = run_weekly(cfg, end_date="2026-09-06", use_llm=True)
    assert result.llm_used and not result.warnings, result.warnings
    out = result.out_dir
    md = (out / "weekly.md").read_text(encoding="utf-8")
    assert "이번 주 다섯 줄" in md and "2026-09-06 헤드라인" in md and "다음 주 볼 것" in md
    naver = (out / "weekly-naver.html").read_text(encoding="utf-8")
    assert "이번 주 부동산 다섯 줄" in naver and "#주간결산" in naver
    # 비용은 weekly 로 구분해 기록된다
    costs = json.loads((cfg.state_dir / "costs.json").read_text(encoding="utf-8"))
    assert costs["entries"][-1]["kind"] == "weekly"
    # INDEX 에 주간 절이 붙는다
    assert "## 주간 결산" in (cfg.output_dir / "INDEX.md").read_text(encoding="utf-8")


def test_사이트에_주간_결산이_실린다(cfg, monkeypatch, tmp_path):
    from rebrief.site import build_site
    from rebrief.weekly import run_weekly
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.weekly.ContentGenerator", FakeGenerator)
    _seed_days(cfg, ["2026-09-02", "2026-09-04", "2026-09-06"])
    run_weekly(cfg, end_date="2026-09-06", use_llm=True)

    dest = build_site(cfg, tmp_path / "site")
    assert (dest / "weekly" / "2026-W36" / "weekly-naver.html").exists()
    assert (dest / "weekly" / "2026-W36" / "weekly.html").exists()
    assert "주간 결산 (1주)" in (dest / "index.html").read_text(encoding="utf-8")
