"""9월 7일에 추가한 것들 — 비용 분리, 자막·컷 리스트, 선정 근거, 반복 감지, 예정 페이지, 발행 기록."""

from __future__ import annotations

import json
import re

from rebrief.models import (
    Article, Cluster, CaptionLine, DailyBrief, IssueBrief,
    LongformScript, LongformSection, ShortsScript, VideoPack,
)


def _article(id_: str, title: str, url: str, publisher: str = "", published=None) -> Article:
    return Article(id=id_, title=title, url=url, feed_id="f", feed_name="테스트피드",
                   publisher=publisher, summary="", body="본문 " * 200, published=published)


def _issue(title: str, urls: list[str], numbers=None) -> IssueBrief:
    return IssueBrief(title=title, one_liner="한 줄", category="가격동향", what_happened=["사실"],
                      numbers=numbers or [], why_it_matters="이유", who_is_affected=[],
                      caution="없음", source_urls=urls)


def _brief(issues, watch=None) -> DailyBrief:
    return DailyBrief(date="2026-09-07", headline="머리글", lead="도입", issues=issues,
                      market_temperature="보통", tomorrow_watch=watch or [])


# ── 1) 호출별 비용 ───────────────────────────────────────


def test_usage_records_each_call_with_its_own_model():
    from rebrief.llm import Usage

    class Resp:
        def __init__(self, inp, out):
            self.usage = type("U", (), {"input_tokens": inp, "output_tokens": out,
                                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})()

    usage = Usage(model="claude-opus-5")
    usage.add(Resp(1000, 10000), "claude-opus-5", kind="브리핑")
    usage.add(Resp(1000, 10000), "claude-sonnet-5", kind="영상 대본")
    kinds = [d["kind"] for d in usage.details]
    assert kinds == ["브리핑", "영상 대본"]
    # 같은 토큰이라도 싼 모델 쪽 비용이 낮아야 한다 (분리의 이유)
    assert usage.details[1]["usd"] < usage.details[0]["usd"]
    assert usage.calls == 2 and "영상 대본" in " ".join(usage.by_kind())


# ── 2·3) 자막 줄바꿈과 컷 리스트 ─────────────────────────


def test_caption_wraps_into_balanced_lines():
    from rebrief.render import wrap_caption

    assert wrap_caption("종부세") == "종부세"                      # 짧으면 그대로
    two = wrap_caption("0~5세 아동 179명이 임대소득을 신고했습니다")
    parts = two.split("\n")
    assert len(parts) == 2 and all(len(p) <= 16 for p in parts)
    assert abs(len(parts[0]) - len(parts[1])) <= 4                 # 한쪽만 길지 않게
    long = wrap_caption("2030년에는 서울 25개구 가운데 22개구가 종부세 과세 대상이 됩니다")
    assert len(long.split("\n")) == 2                              # 넘쳐도 줄 수는 지킨다
    assert "".join(long.split()) == "2030년에는서울25개구가운데22개구가종부세과세대상이됩니다"   # 글자를 버리지 않는다


def test_srt_and_cut_list(tmp_path):
    from rebrief.render import longform_chapter_csv, shorts_cut_csv, to_srt

    lines = [CaptionLine(at="00:00", text="0~5세 아동 179명이 임대소득을 신고했습니다", visual="수치 카드"),
             CaptionLine(at="00:04", text="짧은 자막", visual="드론샷")]
    srt = to_srt(lines, 8)
    assert "00:00:00,000 --> 00:00:04,000" in srt and "\n임대소득을 신고했습니다\n" in srt

    shorts = ShortsScript(title_candidates=["t"], hook="훅", lines=lines, cta="cta",
                          hashtags=["#x"], estimated_seconds=8)
    csv = shorts_cut_csv(shorts, ["img-1-stat-card.png"])
    rows = csv.lstrip("﻿").strip().split("\r\n")
    assert rows[0].startswith("컷,시작(TC)")
    assert "00:00:00:00" in rows[1] and "img-1-stat-card.png" in rows[1]   # 그래픽 컷에만 그림
    assert rows[2].endswith(",")                                            # 드론샷 컷은 비움

    longform = LongformScript(
        title_candidates=["t"], thumbnail_texts=["x"], cold_open="여는 말",
        sections=[LongformSection(chapter="첫 챕터", at="01:20", script="대본", broll=["B롤"], graphics=["11%"])],
        outro="끝", description="설명", tags=["t"], pinned_comment="댓글", estimated_minutes=8.0)
    chapters = longform_chapter_csv(longform).lstrip("﻿").strip().split("\r\n")
    assert "00:01:20:00" in chapters[1] and "첫 챕터" in chapters[1]


# ── 4) 이슈 선정 근거 ────────────────────────────────────


def test_explain_issues_matches_by_source_url_not_order():
    from datetime import datetime, timezone

    from rebrief.render import explain_issues

    a1 = _article("a1", "종부세 확대", "https://x.test/1", "매일경제",
                  datetime(2026, 9, 6, 13, 5, tzinfo=timezone.utc))
    a2 = _article("a2", "종부세 분석", "https://x.test/2", "한국경제")
    b1 = _article("b1", "임대 공급", "https://y.test/1", "뉴스1")
    clusters = [Cluster(key="c1", articles=[a1, a2], score=9.0),
                Cluster(key="c2", articles=[b1], score=5.0)]
    # 모델이 순서를 뒤집어도 근거 주소로 짝지어야 한다
    brief = _brief([_issue("임대 공급 감소", ["https://y.test/1"]),
                    _issue("종부세", ["https://x.test/2"])])
    why = explain_issues(brief, clusters)
    assert why[0].startswith("오늘 2위") and "매체 1곳" in why[0]
    assert why[1].startswith("오늘 1위") and "매체 2곳" in why[1] and "기사 2건" in why[1]
    assert "최신 09-06 22:05" in why[1]          # 한국 시간으로 바꿔 보여 준다


# ── 5·8) 반복 주제 · 읽기 쉬움 · 자막 길이 ───────────────


def test_repeat_and_readability_checks(cfg):
    from rebrief import checklist as cl
    from rebrief.models import BlogPost

    post = BlogPost(title="t", slug="s", meta_description="d", tags=["a"],
                    body_markdown="## 소제목\n\n" + "아주 긴 문장을 이어 붙여서 " * 6 + "끝냅니다.")
    items = {i.key: i for i in cl.build(cfg, brief=None, post=post,
                                        repeats=[{"title": "종부세", "prev_date": "2026-09-05",
                                                  "prev_title": "종부세 확대", "days_ago": 2}])}
    assert items["readability"].level == cl.WARN
    assert items["repeat"].level == cl.WARN and "2일 전" in items["repeat"].title


def test_recent_topics_reads_previous_days(cfg):
    from datetime import date

    from rebrief.store import recent_topics

    day = cfg.output_dir / "2026-09-05"
    day.mkdir(parents=True)
    (day / "data.json").write_text(json.dumps({"issues": [{"title": "종부세 확대"}]}), encoding="utf-8")
    rows = recent_topics(cfg.output_dir, date(2026, 9, 7), days=7)
    assert rows == [("2026-09-05", "종부세 확대")]


def test_long_captions_flags_overflowing_cuts():
    from rebrief import checklist as cl

    lines = [CaptionLine(at="00:00", text="짧다", visual="v"),
             CaptionLine(at="00:03", text="2030년에는 서울 25개구 가운데 22개구가 종부세 과세 대상이 됩니다", visual="v")]
    pack = VideoPack(
        shorts=ShortsScript(title_candidates=["t"], hook="h", lines=lines, cta="c",
                            hashtags=["#x"], estimated_seconds=10),
        longform=LongformScript(title_candidates=["t"], thumbnail_texts=["x"], cold_open="o",
                                sections=[], outro="e", description="d", tags=["t"],
                                pinned_comment="p", estimated_minutes=8.0))
    over = cl.long_captions(pack)
    assert len(over) == 1 and over[0].startswith("2컷 ·")


# ── 6·7) 이번 주 볼 것 · 발행 기록 ───────────────────────


def test_upcoming_page_and_published_badge(cfg, tmp_path):
    from rebrief.site import build_site
    from rebrief.store import PublishLog

    day = cfg.output_dir / "2026-09-07"
    day.mkdir(parents=True)
    (day / "brief.md").write_text("# 브리핑", encoding="utf-8")
    (day / "data.json").write_text(json.dumps({
        "date": "2026-09-07", "headline": "머리글", "issues": [],
        "tomorrow_watch": ["국토부 발표 확인", "국토부 발표 확인하기"],   # 거의 같은 말은 한 번만
    }, ensure_ascii=False), encoding="utf-8")
    log_ = PublishLog(cfg.state_dir / "published.json")
    log_.record("2026-09-07", url="https://blog.naver.com/x/1")
    log_.save()

    dest = build_site(cfg, tmp_path / "site")
    upcoming = (dest / "upcoming.html").read_text(encoding="utf-8")
    assert upcoming.count("국토부 발표 확인") == 1
    index = (dest / "index.html").read_text(encoding="utf-8")
    assert "이번 주 볼 것" in index and "발행함" in index and "blog.naver.com/x/1" in index


# ── 유입 (9/7 오후): 대표 검색어 · 요약·목차 · 지난 글 · 겹침 ──


def test_keyword_placement_reports_missing_spots():
    from rebrief.checklist import keyword_placement
    from rebrief.models import BlogPost

    def post(title, body, kw="종부세 대상 자치구"):
        return BlogPost(title=title, slug="s", meta_description="d", tags=["t"],
                        focus_keyword=kw, body_markdown=body)

    good = post("종부세 대상 자치구 총정리, 9월 7일 브리핑",
                "종부세 대상 자치구가 어디인지부터 봅니다.\n\n## 종부세 대상 자치구는 어디인가\n\n본문")
    assert keyword_placement(good) == ("종부세 대상 자치구", [])

    _, missing = keyword_placement(post("오늘의 부동산 브리핑", "집값 이야기입니다.\n\n## 정리\n\n본문"))
    assert missing == ["제목", "첫 120자", "소제목"]

    _, late = keyword_placement(post("9월 7일 부동산 브리핑에서 살펴본 종부세 대상 자치구",
                                     "종부세 대상 자치구 이야기\n\n## 종부세 대상 자치구\n\n본문"))
    assert late == ["제목 앞쪽(지금은 뒤쪽)"]

    assert keyword_placement(post("제목", "본문", kw="")) == ("", [])


def test_overlap_with_previous_counts_repeated_sentences():
    from rebrief.checklist import overlap_with_previous

    yesterday = "서울 아파트값이 3주 연속 내렸습니다. 낙폭은 오히려 줄었습니다."
    today = "서울 아파트값이 3주 연속 내렸습니다. 오늘은 종부세 대상 자치구가 늘어난다는 분석이 나왔습니다."
    ratio, samples = overlap_with_previous(today, [("2026-09-06", yesterday)])
    assert ratio == 0.5 and samples[0].startswith("서울 아파트값이")
    assert overlap_with_previous(today, [])[0] == 0.0


def test_naver_html_has_summary_outline_question_and_related():
    from rebrief.render import outline_from_markdown, to_naver_html

    body = "첫 문단입니다.\n\n## 첫 소제목\n\n내용\n\n## 둘째 소제목\n\n내용\n\n## 셋째 소제목\n\n내용"
    assert outline_from_markdown(body) == ["첫 소제목", "둘째 소제목", "셋째 소제목"]
    html = to_naver_html(body, summary_lines=["요약 하나", "요약 둘", "요약 셋"],
                         closing_question="여러분은 어떠신가요?",
                         related=[{"date": "2026-09-06", "title": "어제 글", "url": "https://blog.naver.com/x/1"}])
    assert html.index("3줄 요약") < html.index("이 글의 순서") < html.index("첫 문단입니다")
    assert "여러분은 어떠신가요?" in html and html.index("첫 문단입니다") < html.index("함께 보면 좋은 지난 글")
    assert '<a href="https://blog.naver.com/x/1">어제 글</a>' in html
    assert "nocopy" not in html          # 지난 글 링크는 복사에 포함돼야 한다
    assert "이 글의 순서" not in to_naver_html("## 하나\n\n글", summary_lines=[])   # 소제목 3개 미만이면 목차 없음


def test_related_posts_prefers_same_topic_and_skips_unpublished(cfg):
    from rebrief.models import DailyBrief
    from rebrief.related import related_posts
    from rebrief.store import PublishLog

    for day, headline, issue in [("2026-09-04", "청약 경쟁률 상승", "청약 경쟁률"),
                                 ("2026-09-05", "종부세 확대 전망", "종부세 대상 자치구"),
                                 ("2026-09-06", "전월세 시장 정리", "전월세 매물")]:
        d = cfg.output_dir / day
        d.mkdir(parents=True)
        (d / "data.json").write_text(json.dumps({"headline": headline, "issues": [{"title": issue}]},
                                                ensure_ascii=False), encoding="utf-8")
    log_ = PublishLog(cfg.state_dir / "published.json")
    log_.record("2026-09-04", url="https://blog.naver.com/x/4")
    log_.record("2026-09-05", url="https://blog.naver.com/x/5")
    log_.record("2026-09-06")                       # 주소를 안 적은 날은 링크할 수 없다
    log_.save()

    brief = _brief([_issue("종부세 대상 자치구 확대", [])])
    got = related_posts(cfg, "2026-09-07", brief, limit=2)
    assert [r["date"] for r in got] == ["2026-09-05", "2026-09-04"]     # 주제가 가까운 날이 먼저
    assert all(r["url"].startswith("https://") for r in got)
    assert isinstance(brief, DailyBrief)


# ── 유입 2차: 지역명 · 표지 이미지 · 태그 30칸 ──────────────


def test_region_finder_handles_particles_and_lookalikes():
    from rebrief.regions import find_regions

    assert find_regions("서울 25개구 중 22개구가 종부세 대상, 강북·금천·도봉 제외") == \
        ["서울", "강북구", "금천구", "도봉구"]              # 구를 뗀 표기도 정식 이름으로, 나온 순서대로
    assert find_regions("경기 화성과 용인 반도체 배후 수요") == ["화성", "용인"]   # 조사가 붙어도 찾는다
    assert find_regions("성동구 아파트값 상승, 중구청 앞 상가는 공실") == ["성동구"]  # 중구청은 지역이 아니다
    assert find_regions("동작 원리를 설명한 자료") == []       # 지역처럼 보이는 낱말은 뺀다
    assert find_regions("잠실 아파트 신고가, 송파구 거래량 증가") == ["잠실", "송파구"]   # 글에 먼저 나온 순


def test_expand_tags_fills_thirty_slots_with_regions():
    from rebrief.render import expand_tags

    tags = expand_tags(["종부세", " 부동산 ", "종부세"], ["송파구", "강남구"],
                       ["부동산", "부동산뉴스", "아파트"], limit=30)
    assert tags[:3] == ["종부세", "부동산", "송파구"]          # 모델 태그 → 지역 → 고정 순, 중복 제거
    assert "송파구아파트" in tags and "강남구아파트" in tags    # 지역은 두 벌로
    assert len(expand_tags([f"태그{i}" for i in range(40)], ["송파구"], [], limit=30)) == 30


def test_blog_gets_cover_and_expanded_tags(cfg, tmp_path):
    from rebrief.keynumbers import KeyNumber
    from rebrief.models import BlogPost
    from rebrief.render import Renderer

    post = BlogPost(
        title="송파구 종부세 대상 확대 전망", slug="s", meta_description="d",
        focus_keyword="송파구 종부세", summary_lines=["첫 줄 요약입니다."],
        tags=["종부세"], body_markdown="송파구 아파트 이야기입니다.\n\n## 송파구 종부세\n\n본문")
    renderer = Renderer(cfg, tmp_path / "out", "2026-09-07")
    cover = renderer.cover(post, [KeyNumber("22개구", "22개구", "종부세 대상")])
    assert cover == "img-0-cover.svg"
    svg = (tmp_path / "out" / cover).read_text(encoding="utf-8")
    assert "송파구" in svg and ">22개구</text>" in svg      # 제목과 핵심 수치 배지

    html = renderer.blog_naver(post, cover=cover).read_text(encoding="utf-8")
    assert html.index("대표 이미지") < html.index("첫 줄 요약입니다")
    assert "#송파구아파트" in html and "#부동산뉴스" in html   # 지역·고정 태그가 채워진다


# ── 그래픽 카드 틀 · 첨부파일 zip ──────────────────────────


def test_cards_share_one_frame_and_fit_inside():
    from rebrief import images

    ch = {"channel": "부동산 브리핑"}
    card = images.stat_card({"label": "롯데건설 누적 수주액", "value": "4조원", "unit": "",
                             "period": "2026년 누적", "context": "지난해보다 많음", "source": "비즈트리뷴"},
                            "2026-09-07", ch)
    assert "부동산 브리핑" in card.svg and "2026-09-07" in card.svg      # 머리말이 붙는다
    assert card.svg.count(f'fill="{images.CARD}"') >= 1                   # 흰 카드 위에 그린다

    # 각주가 카드 밖으로 나가지 않아야 한다 (예전엔 큰 숫자와 겹쳤다)
    height = float(re.search(r'height="(\d+)"', card.svg).group(1))
    ys = [float(m) for m in re.findall(r'<text[^>]*y="([\d.]+)"', card.svg)]
    assert max(ys) < height - images.M


def test_axis_ticks_are_round_numbers_and_cover_data():
    from rebrief.images import nice_ticks

    ticks = nice_ticks(-0.0744, 0.0344)
    assert ticks == [-0.1, -0.05, 0.0, 0.05]          # 0.0344 같은 숫자를 축에 적지 않는다
    assert min(ticks) <= -0.0744 and max(ticks) >= 0.0344   # 데이터가 눈금 밖으로 나가지 않는다
    assert nice_ticks(0, 4200)[0] == 0


def test_site_bundles_attachments_into_one_zip(cfg, tmp_path):
    import zipfile

    from rebrief.site import build_site

    day = cfg.output_dir / "2026-09-07"
    day.mkdir(parents=True)
    (day / "brief.md").write_text("# 브리핑", encoding="utf-8")
    (day / "img-1-stat-card.png").write_bytes(b"\x89PNG")
    (day / "thumb-shorts.png").write_bytes(b"\x89PNG")
    (day / "script-shorts.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n자막\n", encoding="utf-8")
    (day / "blog.md").write_text("본문", encoding="utf-8")      # 문서는 첨부물이 아니다

    dest = build_site(cfg, tmp_path / "site")
    names = zipfile.ZipFile(dest / "2026-09-07" / "files.zip").namelist()
    assert set(names) == {"img-1-stat-card.png", "thumb-shorts.png", "script-shorts.srt"}
    assert "첨부파일 모두 내려받기" in (dest / "index.html").read_text(encoding="utf-8")
    assert "files.zip" in (dest / "latest" / "images.html").read_text(encoding="utf-8")


# ── 짧게 읽히는 글: 용어 풀이 · 그래서 나는? · 구조 ────────


def test_glossary_picks_terms_in_order_without_duplicates():
    from rebrief.glossary import explain

    text = "오늘의 중심은 종부세(종합부동산세)입니다. 정부는 기업형 임대에 합산배제를 검토합니다."
    got = explain(text, limit=3)
    assert [t for t, _ in got] == ["종합부동산세", "기업형 임대", "합산배제"]   # 나온 순서대로
    assert all(len(m) < 60 for _, m in got)
    assert explain("집값 이야기", limit=3) == []
    assert len(explain(text, limit=1)) == 1


def test_naver_html_shows_glossary_and_takeaways(cfg, tmp_path):
    from rebrief.models import BlogPost
    from rebrief.render import Renderer

    post = BlogPost(
        title="종부세 대상 확대", slug="s", meta_description="d", tags=["종부세"],
        focus_keyword="종부세 대상", summary_lines=["요약 한 줄"],
        takeaways=["무주택 실수요자라면, 대출 한도부터 확인해 보세요.", "1주택자라면 보유세 부담을 계산해 보세요."],
        closing_question="여러분은 어떠신가요?",
        body_markdown="종부세(종합부동산세) 이야기입니다.\n\n## 종부세 대상\n\n본문\n\n## 그 밖의 오늘 소식\n\n- 한 줄\n")
    html = Renderer(cfg, tmp_path / "out", "2026-09-07").blog_naver(post).read_text(encoding="utf-8")
    assert html.index("낯선 말 풀이") < html.index("종부세(종합부동산세) 이야기")   # 본문보다 앞
    assert "가진 집들의 공시가격 합이" in html
    assert html.index("그래서 나는?") > html.index("본문")                        # 본문 뒤
    assert "무주택 실수요자라면" in html and html.index("그래서 나는?") < html.index("여러분은 어떠신가요")


def test_checklist_flags_sprawling_shape_and_missing_takeaways(cfg):
    from rebrief import checklist as cl
    from rebrief.models import BlogPost

    sprawl = BlogPost(title="t", slug="s", meta_description="d", tags=["a"], focus_keyword="집값",
                      body_markdown="\n\n".join(["집값 이야기"] + [f"## 소제목 {i}\n\n내용" for i in range(7)]))
    items = {i.key: i for i in cl.build(cfg, post=sprawl)}
    assert items["shape"].level == cl.WARN and "그 밖의" in items["shape"].title
    assert items["takeaways"].level == cl.WARN

    tight = BlogPost(title="t", slug="s", meta_description="d", tags=["a"], focus_keyword="집값",
                     takeaways=["무주택자라면 …"],
                     body_markdown="집값 이야기\n\n## 집값 흐름\n\n내용\n\n## 그 밖의 오늘 소식\n\n- 한 줄")
    ok = {i.key: i for i in cl.build(cfg, post=tight)}
    assert ok["shape"].level == cl.OK and "takeaways" not in ok


# ── 정부 정책 원문 (정책브리핑) ────────────────────────────

_LIST_HTML = """
<ul>
<li><a href="/briefing/pressReleaseView.do?newsId=111&amp;pageIndex=1">
  <span class="text"><strong>8월중 전세사기피해자등 658건 추가 결정</strong>
  <span class="lead">- 위원회 3회 개최- 누적 40,936건 결정</span>
  <span class="source"><span>2026.09.07</span><span>국토교통부</span></span></span></a></li>
<li><a href="/briefing/pressReleaseView.do?newsId=222&amp;pageIndex=1">
  <span class="text"><strong>아프리카 기상 협력 연수</strong>
  <span class="lead">- 15개국 공무원 대상</span>
  <span class="source"><span>2026.09.07</span><span>기상청</span></span></span></a></li>
</ul>
"""

_VIEW_HTML = """
<div class="view_cont">"이 자료는 국토교통부의 보도자료를 전재하여 제공함을 알려드립니다."</div>
<div class="file">첨부파일
  <span>260908(조간) 전세사기피해자 결정.hwpx</span>
  <a href="/common/download.do?fileId=1&amp;tblKey=GMN">내려받기</a>
  <span>260908(조간) 전세사기피해자 결정.pdf</span>
  <a href="/common/download.do?fileId=2&amp;tblKey=GMN">내려받기</a>
</div>
<div class="article_footer">공유</div>
"""


def test_policy_list_and_detail_parsing():
    from rebrief.policy import parse_detail, parse_list

    docs = parse_list(_LIST_HTML)
    assert [d.news_id for d in docs] == ["111", "222"]
    first = docs[0]
    assert first.title == "8월중 전세사기피해자등 658건 추가 결정"      # 제목만, 요약이 섞이지 않는다
    assert first.dept == "국토교통부" and first.date == "2026-09-07"
    assert "누적 40,936건" in first.lead

    body, files = parse_detail(_VIEW_HTML)
    assert body == ""                                                   # '전재하여 제공' 안내는 본문이 아니다
    assert [f["name"] for f in files] == ["260908(조간) 전세사기피해자 결정.hwpx",
                                          "260908(조간) 전세사기피해자 결정.pdf"]
    assert files[0]["url"].startswith("https://www.korea.kr/common/download.do?fileId=1")


def test_policy_keeps_real_estate_and_drops_the_rest(cfg, monkeypatch):
    from rebrief import policy as P

    pages = {"1": _LIST_HTML}

    class Resp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    def fake_get(url, params=None, **kw):
        if "pressReleaseList" in url:
            return Resp(pages.get((params or {}).get("pageIndex"), ""))
        return Resp(_VIEW_HTML)

    monkeypatch.setattr(P, "_get", fake_get)
    docs = P.fetch(cfg, "2026-09-07", days=1, limit=5)
    assert [d.title for d in docs] == ["8월중 전세사기피해자등 658건 추가 결정"]   # 기상 연수는 뺀다
    assert len(docs[0].files) == 2


def test_policy_summary_uses_document_bullets_not_boilerplate():
    from rebrief.policy import PolicyDoc, doc_chunks, extractive_summary

    doc = PolicyDoc("1", "제목", "국토교통부", "2026-09-07", "u",
                    lead="제목 관련 보도자료 내용입니다. 자세한 내용은 첨부파일을 참고하시기 바랍니다.")
    doc.body = ("보도시점 배포 즉시 2026. 9. 7. 제목입니다 "
                "□ 국토교통부는 8월 한 달간 위원회를 3회 열어 658건을 결정하였다. "
                "ㅇ 누적 40,936건이 결정되었으며 피해주택 10,718호를 매입하였다.")
    lines = extractive_summary(doc)
    assert len(lines) == 2 and "658건" in lines[0] and "40,936건" in lines[1]
    assert "보도시점" not in " ".join(lines)          # 머리말은 요약에 들어가지 않는다
    assert doc_chunks("□ 짧음 ㅇ " + "가" * 30)[0].startswith("가")


def test_policy_block_appears_in_both_blog_files():
    from rebrief.policy import PolicyDoc
    from rebrief.render import policy_block_html, policy_block_markdown

    doc = PolicyDoc("1", "전세사기피해자 658건 추가 결정", "국토교통부", "2026-09-07",
                    "https://www.korea.kr/briefing/pressReleaseView.do?newsId=1")
    doc.files = [{"name": "보도자료.pdf", "url": "https://www.korea.kr/common/download.do?fileId=2"}]

    md = policy_block_markdown([doc])
    assert "[전세사기피해자 658건 추가 결정](https://www.korea.kr/briefing/" in md
    assert "첨부 [보도자료.pdf](https://www.korea.kr/common/download.do?fileId=2)" in md

    html = policy_block_html([doc])
    assert "오늘 나온 정부 발표 원문" in html and "보도자료.pdf</a>" in html
    assert policy_block_markdown([]) == "" and policy_block_html(None) == ""


# ── 공유 카드·구독 피드 ──────────────────────────────────────

def test_share_card_tags_and_feed(cfg, tmp_path):
    import xml.etree.ElementTree as ET

    from rebrief import site as S

    base = S.site_base(cfg)
    tags = S.meta_tags(base, title="오늘의 브리핑", description="설명 줄",
                       path="2026-09-07/brief.html", image="2026-09-07/img-0-cover.png",
                       image_size=(1200, 630), published="2026-09-07", channel="부동산 브리핑")
    assert f'<meta property="og:image" content="{base}2026-09-07/img-0-cover.png">' in tags
    assert '<meta property="og:image:width" content="1200">' in tags
    assert '"@type": "NewsArticle"' in tags and '"datePublished": "2026-09-07"' in tags
    assert 'og:type" content="article"' in tags
    # 주소가 없는 설정에서도 태그는 나오되 이미지는 빼야 한다 (상대 주소 카드는 깨진다)
    plain = S.meta_tags("", title="제목", image="a.png")
    assert "og:image" not in plain and 'twitter:card" content="summary"' in plain

    dest = tmp_path / "site"
    dest.mkdir()
    built = [{"date": "2026-09-07", "headline": "종부세 확대 전망", "description": "한 줄 설명",
              "pages": [{"href": "blog-naver.html"}, {"href": "brief.html"}]}]
    S._build_feed(cfg, built, dest)
    S._build_sitemap(cfg, built, [], dest)
    root = ET.parse(dest / "feed.xml").getroot()
    item = root.find("./channel/item")
    assert item.findtext("title") == "종부세 확대 전망"
    assert item.findtext("link").endswith("/2026-09-07/brief.html")     # 읽는 페이지로 건다
    assert item.findtext("pubDate") == "Mon, 07 Sep 2026 07:00:00 +0900"
    assert ET.parse(dest / "sitemap.xml").getroot().find(
        "{http://www.sitemaps.org/schemas/sitemap/0.9}url") is not None
    assert "Sitemap:" in (dest / "robots.txt").read_text(encoding="utf-8")


def test_feed_date_is_not_localised(monkeypatch):
    """컴퓨터 언어 설정이 한국어여도 구독기가 읽는 영문 날짜가 나와야 한다."""
    import locale

    from rebrief.site import _rfc822

    try:
        locale.setlocale(locale.LC_TIME, "ko_KR.UTF-8")
    except locale.Error:
        pass
    try:
        assert _rfc822("2026-01-01") == "Thu, 01 Jan 2026 07:00:00 +0900"
        assert _rfc822("엉터리") == ""
    finally:
        locale.setlocale(locale.LC_TIME, "C")


# ── 정책 일정·후속 발표 ──────────────────────────────────────

def test_policy_schedule_picks_future_dates_only():
    from rebrief.policy import PolicyDoc, schedule_items

    doc = PolicyDoc("1", "주택공급규칙 개정", "국토교통부", "2026-09-07", "u")
    doc.body = ("□ 국토교통부는 개정안을 9월 15일부터 10월 24일까지 입법예고한다. "
                "ㅇ 개정안은 2026년 12월 1일부터 시행된다. "
                "ㅇ 지난 8월 5일 발표한 대책의 후속이며 총 1,798건을 심의하였다.")
    rows = schedule_items(doc, "2026-09-07")
    dates = [(r["date"], r["kind"]) for r in rows]
    assert ("2026-10-24", "입법예고") in dates      # 기간은 마감일을 남긴다
    assert ("2026-12-01", "시행") in dates
    assert all(r["date"] >= "2026-09-07" for r in rows)   # 8월 5일은 지나갔다
    assert "1,798" not in " ".join(r["text"] for r in rows) or len(rows) == 2

    # 일정 낱말이 없는 숫자 나열은 걸리지 않는다
    plain = PolicyDoc("2", "통계", "국토교통부", "2026-09-07", "u")
    plain.body = "ㅇ 12월 1일 기준 누계는 40,936건이며 10월 24일 기준 1,798건이다."
    assert schedule_items(plain, "2026-09-07") == []


def test_policy_log_links_follow_ups(tmp_path):
    from rebrief.policy import PolicyDoc
    from rebrief.store import PolicyLog

    book = PolicyLog(tmp_path / "policies.json")
    first = PolicyDoc("1", "주택공급 규칙 개정안 입법예고", "국토교통부", "2026-09-01", "u1")
    book.add(first, "2026-09-01", [{"date": "2026-12-01", "kind": "시행", "text": "시행한다",
                                    "title": first.title, "url": "u1"}])
    later = PolicyDoc("2", "주택공급 규칙 개정안 시행", "국토교통부", "2026-09-07", "u2")
    other = PolicyDoc("3", "전세사기피해자 결정", "국토교통부", "2026-09-07", "u3")

    assert [f["news_id"] for f in book.follow_ups(later)] == ["1"]
    assert book.follow_ups(other) == []                    # 다른 정책은 이어 붙이지 않는다
    assert book.follow_ups(first) == []                    # 자기 자신은 뺀다

    assert [i["date"] for i in book.upcoming("2026-09-07")] == ["2026-12-01"]
    assert book.upcoming("2027-01-01") == []               # 지나간 일정은 안 싣는다

    book.save()
    assert PolicyLog(tmp_path / "policies.json").docs.keys() == {"1"}


# ── 네이버 검색 노출 확인 ────────────────────────────────────

def test_index_check_finds_my_post(monkeypatch):
    from rebrief import indexcheck as I

    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")

    class Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    sent = {}

    def fake_get(url, params=None, headers=None, **kw):
        sent["query"] = (params or {}).get("query")
        sent["id"] = (headers or {}).get("X-Naver-Client-Id")
        return Resp({"items": [
            {"link": "https://blog.naver.com/other/222222222222"},
            {"link": "https://m.blog.naver.com/rokiz/223456789012"},
        ]})

    monkeypatch.setattr(I, "_get", fake_get)

    # 모바일 주소로 검색돼도 내가 올린 PC 주소와 같은 글로 본다
    got = I.check_post("종부세 확대 전망", "https://blog.naver.com/rokiz/223456789012")
    assert got == {"indexed": True, "rank": 2, "checked": True}
    assert sent["query"] == "종부세 확대 전망" and sent["id"] == "id"

    미노출 = I.check_post("종부세 확대 전망", "https://blog.naver.com/rokiz/999999999999")
    assert 미노출 == {"indexed": False, "rank": None, "checked": True}

    assert I.post_id("https://blog.naver.com/PostView.naver?logNo=223456789012") == "223456789012"
    assert I.post_id("https://example.com/no-number") == ""


def test_index_check_without_key_is_not_a_failure(cfg, monkeypatch, tmp_path):
    """키가 없을 때 '노출 안 됨' 으로 잘못 기록하면 안 된다."""
    from rebrief import indexcheck as I

    monkeypatch.delenv("NAVER_CLIENT_ID", raising=False)
    monkeypatch.delenv("NAVER_CLIENT_SECRET", raising=False)
    assert I.check_post("제목", "https://blog.naver.com/rokiz/1") == {
        "indexed": False, "rank": None, "checked": False}
    result = I.run(cfg, "2026-09-07")
    assert result["skipped"] is True and result["missing"] == []
    assert I.build_message(result) == ""


def test_index_check_warns_only_after_a_few_days(cfg, monkeypatch):
    from rebrief import indexcheck as I
    from rebrief.store import PublishLog, TitleLog

    book = PublishLog(cfg.state_dir / "published.json")
    book.record("2026-09-01", url="https://blog.naver.com/rokiz/111111111111")
    book.record("2026-09-07", url="https://blog.naver.com/rokiz/222222222222")
    book.save()
    titles = TitleLog(cfg.state_dir / "titles.json")
    titles.record_candidates("2026-09-01", "blog", ["오래된 글"])
    titles.record_candidates("2026-09-07", "blog", ["오늘 글"])
    titles.save()

    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    monkeypatch.setattr(I, "check_post",
                        lambda title, url, **kw: {"indexed": False, "rank": None, "checked": True})

    result = I.run(cfg, "2026-09-07", days_back=10, warn_after=3)
    assert result["checked"] == 2
    # 오늘 올린 글은 아직 안 걸려도 정상이라 알리지 않는다
    assert [r["date"] for r in result["missing"]] == ["2026-09-01"]
    assert "6일째" in I.build_message(result)


# ── 정부 통계 직접 받기 ──────────────────────────────────────

_OLD_XML = """<response><body><items>
<item><아파트>은마</아파트><거래금액> 285,000</거래금액><전용면적>84.43</전용면적>
 <년>2026</년><월>8</월><일>5</일><법정동> 대치동</법정동><층>5</층></item>
<item><아파트>래미안</아파트><거래금액>190,000</거래금액><전용면적>59.9</전용면적>
 <년>2026</년><월>8</월><일>12</일><법정동>도곡동</법정동><층>12</층></item>
</items></body></response>"""

_NEW_XML = """<response><body><items>
<item><aptNm>헬리오시티</aptNm><dealAmount>230,000</dealAmount><excluUseAr>84.99</excluUseAr>
 <dealYear>2026</dealYear><dealMonth>8</dealMonth><dealDay>3</dealDay>
 <umdNm>가락동</umdNm><floor>7</floor></item>
</items></body></response>"""

_ERR_XML = """<OpenAPI_ServiceResponse><cmmMsgHeader>
<errMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg></cmmMsgHeader></OpenAPI_ServiceResponse>"""


def test_trade_parsing_handles_both_tag_styles():
    from rebrief.stats import parse_trades, summarize

    old = parse_trades(_OLD_XML)
    assert [r["name"] for r in old] == ["은마", "래미안"]
    assert old[0]["amount"] == 2_850_000_000            # 285,000만원 → 28.5억
    assert old[0]["area"] == 84.43 and old[0]["date"] == "2026-08-05"
    assert old[0]["dong"] == "대치동"                    # 앞뒤 공백은 지운다

    new = parse_trades(_NEW_XML)
    assert new[0]["name"] == "헬리오시티" and new[0]["amount"] == 2_300_000_000

    assert parse_trades(_ERR_XML) == []                 # 키 오류를 거래 0건으로 읽지 않는다
    assert parse_trades("망가진 XML") == []

    got = summarize(old)
    assert got["count"] == 2 and got["avg"] == 2_375_000_000
    assert got["top"]["name"] == "은마"
    assert summarize([]) == {"count": 0, "avg": 0, "median": 0, "top": None}


def test_stats_collect_compares_with_previous_month(cfg, monkeypatch, tmp_path):
    from rebrief import stats as S
    from rebrief.render import Renderer

    monkeypatch.setenv("DATA_GO_KR_KEY", "테스트키")
    calls = []

    class Resp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    def fake_get(url, params=None, **kw):
        calls.append((params["LAWD_CD"], params["DEAL_YMD"]))
        return Resp(_OLD_XML if params["DEAL_YMD"] == "202608" else _NEW_XML)

    monkeypatch.setattr(S, "_get", fake_get)
    monkeypatch.setitem(cfg.settings, "stats", {
        "enabled": True, "max_districts": 2,
        "districts": [{"name": "강남구", "code": "11680"}, {"name": "송파구", "code": "11710"}]})

    data = S.collect(cfg, "2026-09-07")
    assert data["month"] == "202608" and data["before"] == "202607"   # 신고 기한 때문에 지난달
    assert ("11680", "202608") in calls and ("11680", "202607") in calls
    assert data["total"] == 4 and data["districts"][0]["change"] == 1

    path = Renderer(cfg, tmp_path, "2026-09-07").stats(data)
    body = path.read_text(encoding="utf-8")
    assert "| 강남구 | 2건 | +1 | 23.8억 |" in body.replace("  ", " ") or "23.8억" in body
    assert "은마" in body and "실거래가로 본 2026년 8월" in body

    monkeypatch.delenv("DATA_GO_KR_KEY")
    assert S.collect(cfg, "2026-09-07") == {}          # 키가 없으면 아무것도 하지 않는다


def test_reb_rows_survives_shape_changes():
    from rebrief.stats import _reb_rows

    assert _reb_rows({"RESULT": {"CODE": "ERROR-290"}}) == []
    wrapped = {"SttsApiTblData": [{"head": []}, {"row": [{"DTA_VAL": "101.2"}]}]}
    assert _reb_rows(wrapped) == [{"DTA_VAL": "101.2"}]
    assert _reb_rows({"없음": 1}) == []


def test_upcoming_page_shows_policy_dates(cfg, tmp_path):
    """정부 발표에서 뽑은 시행일이 '이번 주 볼 것' 에 실려야 한다."""
    import json

    from rebrief.site import _build_upcoming, make_env

    (cfg.state_dir).mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "policies.json").write_text(json.dumps({"docs": {"1": {
        "title": "주택공급규칙 개정", "schedule": [
            {"date": "2026-12-01", "kind": "시행", "text": "12월 1일부터 시행된다",
             "title": "주택공급규칙 개정", "url": "https://www.korea.kr/x"},
            {"date": "2026-01-01", "kind": "시행", "text": "지나간 일정",
             "title": "옛 발표", "url": "https://www.korea.kr/y"}],
    }}}, ensure_ascii=False), encoding="utf-8")

    day = cfg.output_dir / "2026-09-07"
    day.mkdir(parents=True, exist_ok=True)
    (day / "data.json").write_text(json.dumps({"tomorrow_watch": ["금통위 발표 확인"]}),
                                   encoding="utf-8")

    dest = tmp_path / "site"
    dest.mkdir()
    count = _build_upcoming(make_env(), [day], dest, cfg=cfg)
    html = (dest / "upcoming.html").read_text(encoding="utf-8")
    assert count == 2
    assert "2026-12-01" in html and "https://www.korea.kr/x" in html
    assert "지나간 일정" not in html          # 오늘보다 이전 일정은 싣지 않는다
    assert "금통위 발표 확인" in html          # 브리핑에서 나온 확인거리도 그대로


def test_data_portal_key_accepts_both_forms(monkeypatch):
    """포털이 보여 주는 Encoding/Decoding 두 벌 중 무엇을 넣어도 되게."""
    from rebrief.stats import deal_key

    monkeypatch.setenv("DATA_GO_KR_KEY", "abc+def/ghi==")
    assert deal_key() == "abc+def/ghi=="
    monkeypatch.setenv("DATA_GO_KR_KEY", "abc%2Bdef%2Fghi%3D%3D")
    assert deal_key() == "abc+def/ghi=="        # 두 번 인코딩되면 '등록되지 않은 키' 가 된다
