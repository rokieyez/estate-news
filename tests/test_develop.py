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
