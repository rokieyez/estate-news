"""9월 7일에 추가한 것들 — 비용 분리, 자막·컷 리스트, 선정 근거, 반복 감지, 예정 페이지, 발행 기록."""

from __future__ import annotations

import json
import re

import pytest

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
        outro="끝", tags=["t"], estimated_minutes=8.0)
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
                                sections=[], outro="e", tags=["t"], estimated_minutes=8.0))
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
    # 머리에는 상자를 하나만 둔다 — 3줄 요약 다음이 바로 본문이다 (2026-09-08).
    assert html.index("3줄 요약") < html.index("첫 문단입니다")
    assert "이 글의 순서" not in html          # 목차는 더 이상 넣지 않는다
    assert "여러분은 어떠신가요?" in html and html.index("첫 문단입니다") < html.index("함께 보면 좋은 지난 글")
    assert '<a href="https://blog.naver.com/x/1">어제 글</a>' in html
    assert "nocopy" not in html          # 지난 글 링크는 복사에 포함돼야 한다


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
    (day / "card-1-cover.png").write_bytes(b"\x89PNG")      # 카드뉴스는 따로 묶인다
    (day / "card-2-numbers.png").write_bytes(b"\x89PNG")
    (day / "script-shorts.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\n자막\n", encoding="utf-8")
    (day / "blog.md").write_text("본문", encoding="utf-8")      # 문서는 첨부물이 아니다

    dest = build_site(cfg, tmp_path / "site")
    # 이름에 날짜가 붙는다 — 며칠치를 받아 두면 files.zip, files-1.zip 이 쌓여
    # 어느 날 것인지 알 수 없었다.
    bundle = dest / "2026-09-07" / "2026-09-07_blogfiles.zip"
    assert bundle.exists() and not (dest / "2026-09-07" / "files.zip").exists()
    names = zipfile.ZipFile(bundle).namelist()
    assert set(names) == {"img-1-stat-card.png", "thumb-shorts.png", "script-shorts.srt"}
    assert "블로그 첨부파일 내려받기" in (dest / "index.html").read_text(encoding="utf-8")
    # latest/ 는 복사본이라 파일 이름은 그대로 그날 날짜를 단다
    assert (dest / "latest" / "2026-09-07_blogfiles.zip").exists()
    assert "2026-09-07_blogfiles.zip" in (dest / "latest" / "images.html").read_text(encoding="utf-8")
    assert 'href="latest/2026-09-07_blogfiles.zip"' in (dest / "index.html").read_text(encoding="utf-8")


def test_site_packs_cards_into_their_own_zip(cfg, tmp_path):
    """카드뉴스는 블로그 첨부물과 다른 봉투에 담는다.

    가는 곳이 다르다 — 첨부물은 네이버 글에, 카드는 유튜브 게시물에 올린다.
    한 봉투에 넣으면 유튜브에 올릴 때마다 자막·컷 리스트를 골라내야 한다.
    """
    import zipfile

    from rebrief.site import build_site

    day = cfg.output_dir / "2026-09-07"
    day.mkdir(parents=True)
    (day / "brief.md").write_text("# 브리핑", encoding="utf-8")
    (day / "img-1-stat-card.png").write_bytes(b"\x89PNG")
    (day / "card-1-cover.png").write_bytes(b"\x89PNG")
    (day / "card-2-numbers.png").write_bytes(b"\x89PNG")

    dest = build_site(cfg, tmp_path / "site")
    cards = dest / "2026-09-07" / "2026-09-07_card_news.zip"
    assert cards.exists()
    assert set(zipfile.ZipFile(cards).namelist()) == {"card-1-cover.png", "card-2-numbers.png"}
    # 블로그 봉투에는 카드가 없다
    blog = zipfile.ZipFile(dest / "2026-09-07" / "2026-09-07_blogfiles.zip").namelist()
    assert not [n for n in blog if n.startswith("card-")]
    index = (dest / "index.html").read_text(encoding="utf-8")
    assert 'href="latest/2026-09-07_card_news.zip"' in index and "카드뉴스 내려받기" in index
    assert "2026-09-07_card_news.zip" in (dest / "latest" / "images.html").read_text(encoding="utf-8")


def test_site_omits_card_zip_when_there_are_no_cards(cfg, tmp_path):
    """카드가 없는 날엔 빈 봉투를 만들지 않는다 (images.cards: false 인 경우)."""
    from rebrief.site import build_site

    day = cfg.output_dir / "2026-09-07"
    day.mkdir(parents=True)
    (day / "brief.md").write_text("# 브리핑", encoding="utf-8")
    (day / "img-1-stat-card.png").write_bytes(b"\x89PNG")

    dest = build_site(cfg, tmp_path / "site")
    assert not (dest / "2026-09-07" / "2026-09-07_card_news.zip").exists()
    assert "카드뉴스 내려받기" not in (dest / "index.html").read_text(encoding="utf-8")


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
    # 낯선 말 풀이 상자는 뺐다 — 본문이 이미 괄호로 설명하고 있어 겹쳤다 (2026-09-08).
    assert "낯선 말 풀이" not in html
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
        return Resp(_OLD_XML if params["DEAL_YMD"] == "202607" else _NEW_XML)

    monkeypatch.setattr(S, "_get", fake_get)
    monkeypatch.setitem(cfg.settings, "stats", {
        "enabled": True, "max_districts": 2,
        "districts": [{"name": "강남구", "code": "11680"}, {"name": "송파구", "code": "11710"}]})

    data = S.collect(cfg, "2026-09-07")
    # 9월 7일에는 8월 신고가 아직 차는 중이라 7월이 마지막 완성 달이다
    assert data["month"] == "202607" and data["before"] == "202606"
    assert ("11680", "202607") in calls and ("11680", "202606") in calls
    assert data["total"] == 4 and data["districts"][0]["change"] == 1

    path = Renderer(cfg, tmp_path, "2026-09-07").stats(data)
    body = path.read_text(encoding="utf-8")
    assert "| 강남구 | 2건 | +1 | 23.8억 |" in body.replace("  ", " ") or "23.8억" in body
    assert "은마" in body and "실거래가로 본 2026년 7월" in body

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


def test_stats_skips_cancelled_deals_and_incomplete_months():
    from rebrief.stats import month_of, parse_trades

    xml = """<response><body><items>
    <item><aptNm>정상</aptNm><dealAmount>100,000</dealAmount><excluUseAr>84.0</excluUseAr>
     <dealYear>2026</dealYear><dealMonth>7</dealMonth><dealDay>1</dealDay><cdealType></cdealType></item>
    <item><aptNm>해제된거래</aptNm><dealAmount>900,000</dealAmount><excluUseAr>84.0</excluUseAr>
     <dealYear>2026</dealYear><dealMonth>7</dealMonth><dealDay>2</dealDay><cdealType>O</cdealType></item>
    </items></body></response>"""
    rows = parse_trades(xml)
    assert [r["name"] for r in rows] == ["정상"]     # 취소된 계약은 거래로 세지 않는다

    # 신고 기한(계약 후 30일)이 지난 달만 본다
    assert month_of("2026-09-07") == "202607"       # 8월은 아직 차는 중
    assert month_of("2026-10-01") == "202608"       # 8월 신고 기한이 지났다
    assert month_of("2026-01-05") == "202511"       # 해를 넘어가도 맞아야 한다


def test_reb_series_asks_for_recent_periods_and_dedupes(cfg, monkeypatch):
    """인증키가 없으면 서버가 쪽 넘김을 무시하므로, 구간을 좁히고 중복을 걸러야 한다."""
    from rebrief import stats as S

    monkeypatch.delenv("REB_API_KEY", raising=False)
    asked = []

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"SttsApiTblData": [{"head": []}, {"row": [
                {"WRTTIME_IDTFR_ID": "202634", "DTA_VAL": 100.5, "CLS_NM": "전국",
                 "WRTTIME_DESC": "2026-08-17"},
                {"WRTTIME_IDTFR_ID": "202635", "DTA_VAL": "100.64", "CLS_NM": "전국",
                 "WRTTIME_DESC": "2026-08-24"},
            ]}]}

    def fake_get(url, params=None, **kw):
        asked.append(params)
        return Resp()

    monkeypatch.setattr(S, "_get", fake_get)
    rows = S.reb_series(cfg, "T244183132827305", "WK", count=12, region_id="50001",
                        run_date="2026-09-07")
    assert [r["time"] for r in rows] == ["202634", "202635"]     # 같은 줄이 쌓이지 않는다
    assert rows[1]["value"] == 100.64 and rows[0]["when"] == "2026-08-17"

    first = asked[0]
    assert first["pSize"] == "5" and "KEY" not in first          # 키가 없으면 견본 크기
    assert first["CLS_ID"] == "50001" and first["DTACYCLE_CD"] == "WK"
    # 견본은 구간의 앞쪽 5건만 주므로 최근 5주만 요청해야 최신 값이 온다
    assert first["START_WRTTIME"] == "202632" and first["END_WRTTIME"] == "202637"
    assert len(asked) == 2                                       # 새 시점이 없으면 멈춘다


def test_week_id_matches_reb_numbering():
    from datetime import date

    from rebrief.stats import week_id

    assert week_id(date(2026, 8, 31)) == "202636"    # 실제 응답의 WRTTIME_IDTFR_ID 와 같다
    assert week_id(date(2026, 5, 11)) == "202620"


def test_reb_series_falls_back_to_sample_when_key_rejected(cfg, monkeypatch):
    """승인 대기 중인 키로도 최근 추이는 보여야 한다."""
    from rebrief import stats as S

    monkeypatch.setenv("REB_API_KEY", "아직-승인-안-된-키")
    calls = []

    class Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def fake_get(url, params=None, **kw):
        calls.append(params)
        if "KEY" in params:                       # 인증 실패 응답
            return Resp({"RESULT": {"CODE": "ERROR-290", "MESSAGE": "인증키가 유효하지 않습니다"}})
        return Resp({"SttsApiTblData": [{"head": []}, {"row": [
            {"WRTTIME_IDTFR_ID": "202636", "DTA_VAL": 100.73, "CLS_NM": "전국",
             "WRTTIME_DESC": "2026-08-31"}]}]})

    monkeypatch.setattr(S, "_get", fake_get)
    rows = S.reb_series(cfg, "T244183132827305", "WK", count=12, region_id="50001",
                        run_date="2026-09-07")
    assert [r["value"] for r in rows] == [100.73]
    assert "KEY" in calls[0] and "KEY" not in calls[-1]     # 키로 먼저, 안 되면 견본으로


# ── 눈에 띄는 거래 · 글에 넣기 ───────────────────────────────

def _deal(name, amount, area=84.5, date="2026-07-10", seq="11680-1"):
    return {"name": name, "dong": "대치동", "seq": seq, "amount": amount,
            "area": area, "floor": "5", "date": date}


def test_highlights_need_enough_history_and_same_size():
    from rebrief.stats import area_bucket, highlights

    assert area_bucket(84.43) == area_bucket(84.99) == 85     # 5㎡ 칸으로 묶는다
    assert area_bucket(59.9) != area_bucket(84.5)

    history = [_deal("은마", 2_000_000_000), _deal("은마", 2_100_000_000),
               _deal("은마", 2_200_000_000)]
    rows = highlights([_deal("은마", 2_600_000_000)], history, district="강남구")
    assert len(rows) == 1
    assert rows[0]["kind"] == "신고가" and rows[0]["pct"] == 18.2
    assert rows[0]["before"] == 2_200_000_000 and rows[0]["district"] == "강남구"

    # 지난 거래가 1건뿐이면 '신고가' 라 부를 근거가 약하다
    assert highlights([_deal("은마", 9_000_000_000)], history[:1]) == []

    # 같은 단지라도 면적 칸이 다르면 견주지 않는다
    small = [_deal("은마", 1_000_000_000, area=59.9) for _ in range(3)]
    assert highlights([_deal("은마", 2_600_000_000)], small) == []


def test_highlights_report_big_average_moves():
    from rebrief.stats import highlights

    history = [_deal("래미안", 1_000_000_000), _deal("래미안", 1_020_000_000)]
    current = [_deal("래미안", 1_180_000_000), _deal("래미안", 1_200_000_000)]
    rows = highlights(current, history)
    assert rows[0]["kind"] == "신고가"        # 최고가를 넘었으면 신고가가 먼저다

    # 최고가는 못 넘었지만 평균이 크게 내린 경우
    down = [_deal("래미안", 850_000_000), _deal("래미안", 860_000_000)]
    rows = highlights(down, history)
    assert rows[0]["kind"] == "급락" and rows[0]["pct"] < -8


def test_stats_block_goes_into_both_blog_files():
    from rebrief.render import stats_block_html, stats_block_markdown

    data = {
        "month_label": "2026년 7월", "before_label": "2026년 6월",
        "total": 2230, "total_before": 2252,
        "districts": [
            {"name": "노원구", "change": 50, "now": {"count": 723, "avg": 700000000}},
            {"name": "강서구", "change": 18, "now": {"count": 387, "avg": 980000000}},
            {"name": "송파구", "change": 30, "now": {"count": 308, "avg": 2260000000}},
            {"name": "은평구", "change": 25, "now": {"count": 283, "avg": 910000000}},
        ],
        "highlights": [
            {"kind": "신고가", "district": "성동구", "name": "벽산", "area": 84.8,
             "amount": 1_080_000_000, "before": 860_000_000, "pct": 25.6},
            {"kind": "급락", "district": "노원구", "name": "상계주공", "area": 41.3,
             "amount": 400_000_000, "before": 450_000_000, "pct": -11.1},
        ],
    }
    html = stats_block_html(data, image="img-stats-volume.png")
    assert "직접 센 숫자 — 2026년 7월" in html
    assert "<b>2230건</b>" in html and "-22건" in html
    assert "노원구 723건(+50)" in html
    assert "벽산" in html and "10.8억" in html and "+25.6%" in html
    assert "상계주공" not in html                    # 신고가만 싣는다
    assert 'class="preview nocopy"' in html          # 미리보기는 복사에서 빠진다

    md = stats_block_markdown(data, image="img-stats-volume.png")
    assert "**2230건**" in md and "![지역별 거래 건수](img-stats-volume.png)" in md
    assert stats_block_html(None) == "" and stats_block_markdown({}) == ""


def test_pipeline_puts_trade_numbers_into_the_post(cfg, monkeypatch):
    """실거래가 자료가 있으면 블로그 글 안에 그 숫자가 들어가야 한다."""
    from tests.test_pipeline import FakeGenerator, RUN_DATE

    from rebrief import pipeline
    from rebrief import stats as S

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DATA_GO_KR_KEY", "테스트키")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    monkeypatch.setitem(cfg.settings, "stats", {
        "enabled": True, "max_districts": 1, "history_months": 2, "reb_statbl_id": "",
        "districts": [{"name": "강남구", "code": "11680"}]})

    class Resp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    def fake_get(url, params=None, **kw):
        return Resp(_NEW_XML if params["DEAL_YMD"].endswith("07") else _OLD_XML)

    monkeypatch.setattr(S, "_get", fake_get)

    result = pipeline.run(cfg, run_date=RUN_DATE, use_llm=True)
    assert not result.warnings, result.warnings

    out = cfg.output_dir / RUN_DATE
    naver = (out / "blog-naver.html").read_text(encoding="utf-8")
    md = (out / "blog.md").read_text(encoding="utf-8")
    assert "직접 센 숫자" in naver and "직접 센 숫자" in md
    assert (out / "stats.md").exists()


# ── 기사 지역 연결 · 이력 · 주간 요약 ────────────────────────

def test_focus_regions_come_first_in_the_table(cfg):
    from rebrief.stats import districts_for

    monkey = {"enabled": True, "max_districts": 4,
              "districts": [{"name": "강남구", "code": "11680"}, {"name": "서초구", "code": "11650"}]}
    cfg.settings["stats"] = monkey

    rows = districts_for(cfg, focus=["노원구", "강남구"])
    assert [r["name"] for r in rows] == ["노원구", "강남구", "서초구"]   # 겹치면 한 번만
    assert rows[0]["focus"] is True and rows[2]["focus"] is False
    assert rows[0]["code"] == "11350"

    assert [r["name"] for r in districts_for(cfg)] == ["강남구", "서초구"]
    # 서울 밖 지역은 코드를 모르므로 조용히 지나간다
    assert [r["name"] for r in districts_for(cfg, focus=["부산"])] == ["강남구", "서초구"]


def test_pipeline_reads_regions_from_article_titles():
    from rebrief.models import Article, Cluster
    from rebrief.pipeline import _focus_regions

    def art(title):
        return Article(id=title, title=title, url="https://e.test/" + title,
                       feed_id="f", feed_name="f")

    issues = [Cluster(key="a", articles=[art("노원구 상계주공 신고가"), art("노원 재건축 속도")]),
              Cluster(key="b", articles=[art("강남구 아파트값 상승")])]
    assert _focus_regions(issues) == ["노원구", "강남구"]
    assert _focus_regions([]) == []


def test_trade_log_keeps_history_and_weekly_summary(tmp_path):
    from rebrief.store import TradeLog

    book = TradeLog(tmp_path / "trades.json")
    data = {"month": "202607", "total": 2200,
            "districts": [{"name": "노원구", "now": {"count": 700, "avg": 700000000}},
                          {"name": "강남구", "now": {"count": 160, "avg": 3200000000}}]}
    book.add("2026-09-01", data, {"매매": [{"value": 100.4}], "전세": [{"value": 100.5}]})
    book.add("2026-09-07", {**data, "total": 2230,
                            "districts": [{"name": "노원구", "now": {"count": 723, "avg": 700000000}},
                                          {"name": "강남구", "now": {"count": 161, "avg": 3290000000}}]},
             {"매매": [{"value": 100.73}], "전세": [{"value": 100.84}]})
    book.save()

    again = TradeLog(tmp_path / "trades.json")
    assert [r["count"] for r in again.month_series("노원구")] == [700, 723]

    week = again.week_summary("2026-09-07")
    assert week["total"] == 2230 and week["total_change"] == 30
    assert week["districts"][0]["name"] == "노원구"
    assert week["index"]["매매"] == {"value": 100.73, "change": 0.33}
    assert again.week_summary("2027-01-01") == {}      # 그 주에 집계가 없으면 빈 값


def test_index_table_aligns_two_series():
    from rebrief.render import index_table

    table = index_table({
        "매매": [{"when": "2026-08-24", "value": 100.64, "region": "전국"},
               {"when": "2026-08-31", "value": 100.73, "region": "전국"}],
        "전세": [{"when": "2026-08-31", "value": 100.84, "region": "전국"}],
    })
    assert table["names"] == ["매매", "전세"] and table["region"] == "전국"
    assert table["lines"][0] == "| 기준일 | 매매 | 전세 |"
    assert table["lines"][2] == "| 2026-08-24 | 100.64 | — |"   # 없는 값은 줄표
    assert table["lines"][3] == "| 2026-08-31 | 100.73 | 100.84 |"
    assert index_table({}) == {}


def test_notification_carries_the_trade_numbers():
    from rebrief.notify import build_run_message

    text = build_run_message(
        date="2026-09-07", headline="종부세 확대 전망", issues=5, articles=300,
        site_url="https://www.rokiz.net/estate-news/", warnings=[], llm_used=True, images=3,
        stats={"month_label": "2026년 7월", "total": 2230, "total_before": 2252,
               "districts": [{}],
               "highlights": [{"kind": "신고가", "district": "성동구", "name": "벽산",
                               "amount": 1_080_000_000, "pct": 25.6}]})
    assert "신고 매매 2,230건 (-22건)" in text
    assert "신고가 성동구 벽산 10.8억 (+25.6%)" in text
    # 통계가 없는 날에도 알림은 그대로 간다
    plain = build_run_message(date="2026-09-07", headline="", issues=1, articles=10,
                              site_url="", warnings=[], llm_used=True)
    assert "🏢" not in plain


# ── 전월세 실거래 · 전세가율 ─────────────────────────────────

_RENT_OLD = """<response><body><items>
<item><아파트>은마</아파트><보증금액> 60,000</보증금액><월세금액>0</월세금액>
 <전용면적>84.43</전용면적><년>2026</년><월>7</월><일>5</일><법정동>대치동</법정동></item>
<item><아파트>은마</아파트><보증금액>10,000</보증금액><월세금액>150</월세금액>
 <전용면적>84.43</전용면적><년>2026</년><월>7</월><일>9</일><법정동>대치동</법정동></item>
</items></body></response>"""

_RENT_NEW = """<response><body><items>
<item><aptNm>헬리오시티</aptNm><deposit>90,000</deposit><monthlyRent>0</monthlyRent>
 <excluUseAr>84.99</excluUseAr><dealYear>2026</dealYear><dealMonth>7</dealMonth>
 <dealDay>3</dealDay><umdNm>가락동</umdNm><aptSeq>11710-9</aptSeq></item>
</items></body></response>"""


def test_rent_parsing_handles_both_tag_styles():
    from rebrief.stats import parse_rents

    old = parse_rents(_RENT_OLD)
    assert [r["deposit"] for r in old] == [600_000_000, 100_000_000]
    assert old[0]["monthly"] == 0 and old[1]["monthly"] == 1_500_000
    assert old[0]["seq"] == "대치동|은마"          # 예전 판에는 단지 번호가 없다

    new = parse_rents(_RENT_NEW)
    assert new[0]["deposit"] == 900_000_000 and new[0]["seq"] == "11710-9"
    assert parse_rents(_ERR_XML) == []


def test_jeonse_ratio_uses_pure_jeonse_only():
    from rebrief.stats import jeonse_ratio

    trades = [_deal("은마", 2_000_000_000, seq="A"), _deal("래미안", 1_000_000_000, seq="B")]
    rents = [
        {"name": "은마", "dong": "대치동", "seq": "A", "deposit": 1_200_000_000,
         "monthly": 0, "area": 84.5, "date": "2026-07-05"},
        # 월세가 붙은 계약은 보증금이 낮아 섞으면 비율이 무너진다
        {"name": "은마", "dong": "대치동", "seq": "A", "deposit": 100_000_000,
         "monthly": 1_500_000, "area": 84.5, "date": "2026-07-09"},
        {"name": "래미안", "dong": "도곡동", "seq": "B", "deposit": 700_000_000,
         "monthly": 0, "area": 84.5, "date": "2026-07-11"},
    ]
    got = jeonse_ratio(trades, rents)
    assert got["count"] == 2 and got["median"] == 65.0
    assert got["pairs"][0]["name"] == "래미안" and got["pairs"][0]["ratio"] == 70.0

    # 짝지을 단지가 하나뿐이면 내놓지 않는다
    assert jeonse_ratio(trades[:1], rents[:1]) == {}
    assert jeonse_ratio(trades, []) == {}


def test_video_prompt_carries_trade_numbers():
    from rebrief.prompts import stats_context

    text = stats_context({
        "month_label": "2026년 7월", "before_label": "2026년 6월",
        "total": 2230, "total_before": 2252,
        "districts": [{"name": "노원구", "change": 50, "now": {"count": 723, "avg": 700000000}}],
        "highlights": [{"kind": "신고가", "district": "성동구", "name": "벽산", "area": 84.8,
                        "amount": 1_080_000_000, "before": 860_000_000, "pct": 25.6}]})
    assert "노원구: 723건 (전달 대비 +50건), 평균 7.0억" in text
    assert "[신고가] 성동구 벽산 84.8㎡ 10.8억" in text
    assert "숫자를 바꾸지 말고 그대로 인용" in text
    assert "지역 전체가 올랐다는 뜻이 아닙니다" in text     # 신고가를 과장해 읽지 않게
    assert stats_context(None) == ""


def test_warns_when_trade_data_goes_stale(cfg, tmp_path):
    from rebrief.pipeline import RunResult, _warn_if_stats_stale
    from rebrief.store import TradeLog

    result = RunResult(date="2026-09-07", out_dir=tmp_path)
    _warn_if_stats_stale(cfg, "2026-09-07", result, reason="키 없음")
    assert "한 번도 받지 못했습니다" in result.warnings[0]

    book = TradeLog(cfg.state_dir / "trades.json")
    book.add("2026-09-06", {"month": "202607", "total": 10,
                            "districts": [{"name": "강남구", "now": {"count": 10, "avg": 1}}]}, {})
    book.save()

    fresh = RunResult(date="2026-09-07", out_dir=tmp_path)
    _warn_if_stats_stale(cfg, "2026-09-07", fresh, reason="키 없음")
    assert fresh.warnings == []          # 하루 빠진 것은 흔한 일이라 알리지 않는다

    late = RunResult(date="2026-09-11", out_dir=tmp_path)
    _warn_if_stats_stale(cfg, "2026-09-11", late, reason="키 없음")
    assert "5일째 받지 못했습니다" in late.warnings[0]


def test_jeonse_ratio_drops_renewal_contracts():
    """갱신 계약은 종전 보증금을 따라가 시세보다 낮다 — 섞으면 비율이 내려간다."""
    from rebrief.stats import jeonse_ratio

    trades = [_deal("은마", 2_000_000_000, seq="A")]
    base = {"name": "은마", "dong": "대치동", "seq": "A", "area": 84.5,
            "monthly": 0, "date": "2026-07-05"}
    rents = [
        {**base, "deposit": 1_000_000_000, "contract": "신규"},
        {**base, "deposit": 1_000_000_000, "contract": "신규"},
        {**base, "deposit": 200_000_000, "contract": "갱신"},   # 오래전 보증금
    ]
    got = jeonse_ratio(trades, rents, min_pairs=1)
    assert got["pairs"][0]["ratio"] == 50.0        # 갱신을 섞었다면 36.7% 가 됐다


def test_trade_pages_are_followed_to_the_end(cfg, monkeypatch):
    """한 쪽에 1000건이 상한이라 거래가 많은 구는 넘겨 받아야 한다."""
    from rebrief import stats as S

    monkeypatch.setenv("DATA_GO_KR_KEY", "테스트키")
    asked = []

    def page_xml(count: int, total: int) -> str:
        items = "".join(
            f"<item><aptNm>단지{i}</aptNm><dealAmount>100,000</dealAmount>"
            f"<excluUseAr>84.0</excluUseAr><dealYear>2026</dealYear><dealMonth>7</dealMonth>"
            f"<dealDay>1</dealDay></item>" for i in range(count))
        return f"<response><body><items>{items}</items>" \
               f"<totalCount>{total}</totalCount></body></response>"

    class Resp:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    def fake_get(url, params=None, **kw):
        asked.append(params["pageNo"])
        return Resp(page_xml(1000 if params["pageNo"] == "1" else 359, 1359))

    monkeypatch.setattr(S, "_get", fake_get)
    rows = S.apt_trades(cfg, "11680", "202607")
    assert len(rows) == 1359 and asked == ["1", "2"]     # 두 쪽이면 충분하다


# ── 전세가율 쌓기 · 비용 감시 · 검색 색인 ────────────────────

def test_trade_log_keeps_jeonse_history(tmp_path):
    from rebrief.store import TradeLog

    book = TradeLog(tmp_path / "trades.json")
    for day, median, count in (("2026-09-05", 54.1, 120), ("2026-09-06", 54.8, 128)):
        book.add(day, {"month": "202607", "total": 2200,
                       "districts": [{"name": "노원구", "now": {"count": 700, "avg": 7e8}}],
                       "jeonse": [{"name": "노원구", "median": median, "count": count}],
                       "highlights": [{"kind": "신고가"}]}, {})
    book.save()

    rows = TradeLog(tmp_path / "trades.json").jeonse_series("노원구")
    assert [r["median"] for r in rows] == [54.1, 54.8]
    assert rows[-1]["count"] == 128            # 표본 수도 함께 — 적은 날은 덜 믿는다
    assert TradeLog(tmp_path / "trades.json").jeonse_series("없는구") == []


def test_costly_day_is_flagged(cfg, tmp_path):
    from types import SimpleNamespace

    from rebrief.pipeline import RunResult, _warn_if_costly
    from rebrief.store import CostLog

    book = CostLog(cfg.state_dir / "costs.json")
    for usd in (0.30, 0.32, 0.28):
        book.entries.append({"date": "2026-09-0", "kind": "daily", "usd": usd})
    book.save()
    assert book.typical() == 0.30              # 가운뎃값

    result = RunResult(date="2026-09-07", out_dir=tmp_path)
    result.usage = SimpleNamespace(calls=3, estimated_usd=0.95)
    _warn_if_costly(cfg, result)
    assert "3.2배" in result.warnings[0] and "원" in result.warnings[0]

    normal = RunResult(date="2026-09-07", out_dir=tmp_path)
    normal.usage = SimpleNamespace(calls=3, estimated_usd=0.35)
    _warn_if_costly(cfg, normal)
    assert normal.warnings == []

    # 견줄 이력이 없으면 아무 말도 하지 않는다 (첫날을 비싸다고 할 수 없다)
    assert CostLog(tmp_path / "none.json").typical() == 0.0


def test_search_index_includes_trade_stats(tmp_path):
    import json

    from rebrief.site import _stats_entries

    day = tmp_path / "2026-09-07"
    day.mkdir()
    assert _stats_entries(day) == []           # 통계가 없는 날은 조용히 넘어간다

    (day / "stats.json").write_text(json.dumps({
        "month_label": "2026년 7월", "before_label": "2026년 6월",
        "total": 2230, "total_before": 2252,
        "districts": [{"name": "노원구", "now": {"count": 723, "avg": 700000000}}],
        "jeonse": [{"name": "노원구", "median": 54.8, "count": 128}],
        "highlights": [{"kind": "신고가", "district": "성동구", "name": "벽산",
                        "amount": 1_080_000_000}],
    }, ensure_ascii=False), encoding="utf-8")

    entry = _stats_entries(day)[0]
    assert entry["href"] == "stats.html" and entry["category"] == "실거래"
    labels = [n["label"] for n in entry["numbers"]]
    assert "노원구 전세가율" in labels and "노원구 거래" in labels
    assert "성동구 벽산 신고가" in labels
    assert "2,230건" in entry["one_liner"]


# ── 월세 구성 · 면적대 · 자치구 지도 · 월간 결산 ──────────────

def test_rent_mix_counts_renewals_but_prices_only_new():
    from rebrief.stats import rent_mix

    rents = [
        {"deposit": 500_000_000, "monthly": 0, "contract": "신규"},
        {"deposit": 700_000_000, "monthly": 0, "contract": "신규"},
        {"deposit": 100_000_000, "monthly": 800_000, "contract": "신규"},
        {"deposit": 300_000_000, "monthly": 0, "contract": "갱신"},   # 종전 조건 — 시세가 아니다
    ]
    mix = rent_mix(rents)
    assert mix["total"] == 4 and mix["renew_count"] == 1
    assert mix["monthly_count"] == 1 and mix["monthly_share"] == 25.0
    # 갱신(3억)을 섞었다면 5억이 됐을 값. 신규 둘의 가운뎃값이라 6억이어야 한다.
    assert mix["jeonse_deposit"] == 600_000_000
    assert mix["jeonse_count"] == 2
    assert mix["rent_monthly"] == 800_000
    assert rent_mix([]) == {}


def test_size_bands_split_at_60_85_135():
    from rebrief.stats import size_band, size_change, size_mix

    assert (size_band(60), size_band(60.1)) == ("소형", "중형")
    assert (size_band(85), size_band(85.1)) == ("중형", "중대형")
    assert (size_band(135), size_band(135.1)) == ("중대형", "대형")

    now = size_mix([{"area": 59, "amount": 500_000_000},
                    {"area": 84, "amount": 900_000_000},
                    {"area": 84, "amount": 1_100_000_000}])
    assert [b["band"] for b in now] == ["소형", "중형", "중대형", "대형"]
    assert now[1]["count"] == 2 and now[1]["share"] == 66.7
    assert now[1]["avg"] == 1_000_000_000

    was = size_mix([{"area": 59, "amount": 400_000_000}])
    merged = size_change(now, was)
    assert merged[0]["was_share"] == 100.0 and merged[0]["share_change"] == -66.7
    # 전달 자료가 없으면 비교를 비워 둔다 (0 으로 두면 '전달엔 없었다' 로 잘못 읽힌다)
    assert size_change(now, [])[0]["was_share"] is None
    assert size_mix([]) == []


def test_choropleth_needs_most_of_seoul():
    from rebrief import images

    names = [gu if gu.endswith("구") else f"{gu}구"
             for row in images.SEOUL_LAYOUT for gu in row.values()]
    half = {n: 100 for n in names[:10]}
    assert images.district_choropleth({"month_label": "2026년 7월", "map": half}, "2026-09-07") is None

    full = {n: 40 + i * 30 for i, n in enumerate(names)}
    img = images.district_choropleth({"month_label": "2026년 7월", "map": full}, "2026-09-07")
    assert img is not None and img.slug == "stats-map"
    # 색만으로 구분하지 않는다 — 칸마다 숫자가 적혀 있어야 한다
    assert f"{full['노원구']:,}" in img.svg and "자료 없음" not in img.svg
    assert images.BLUE_RAMP[-1] in img.svg


def test_monthly_review_skips_thin_months(cfg):
    from rebrief.monthly import collect_month, prev_month_of, run_monthly

    assert prev_month_of("2026-09-07") == "2026-08"
    assert prev_month_of("2026-01-03") == "2025-12"

    for day in ("2026-08-03", "2026-08-04"):
        out = cfg.output_dir / day
        out.mkdir(parents=True)
        (out / "data.json").write_text('{"headline": "테스트"}', encoding="utf-8")
    assert [d["date"] for d in collect_month(cfg, "2026-08")] == ["2026-08-03", "2026-08-04"]
    assert collect_month(cfg, "2026-07") == []

    result = run_monthly(cfg, month="2026-08", use_llm=False)
    assert result.skipped and not result.files
    assert "2일치" in result.warnings[0]


def test_monthly_review_writes_article(cfg, monkeypatch):
    from rebrief import monthly as monthly_mod
    from rebrief.models import MonthlyReview
    from rebrief.store import TradeLog

    for day in range(1, 13):
        out = cfg.output_dir / f"2026-08-{day:02d}"
        out.mkdir(parents=True)
        (out / "data.json").write_text('{"headline": "테스트", "issues": []}', encoding="utf-8")

    book = TradeLog(cfg.state_dir / "trades.json")
    book.add("2026-08-28", {"month": "202606", "total": 2252,
                            "districts": [{"name": "노원구", "now": {"count": 673, "avg": 7e8}}],
                            "jeonse": [{"name": "노원구", "median": 54.8, "count": 120}],
                            "highlights": []}, {})
    book.save()
    assert monthly_mod.month_trades(cfg, "2026-08")["month_label"] == "2026년 6월"
    assert monthly_mod.month_trades(cfg, "2026-05") == {}

    class FakeGenerator:
        def __init__(self, _cfg):
            from rebrief.llm import Usage

            self.usage = Usage(cfg)

        def generate_monthly(self, days, label, trades=None):
            assert trades and trades["total"] == 2252     # 장부를 그대로 넘겨야 한다
            return MonthlyReview(
                title="2026년 8월 부동산 결산", slug="2026-08", meta_description="여덟 달째 정리",
                month_lines=["한 줄"] * 5, body_markdown="## 소제목\n\n본문.\n",
                turning_points=["8월 12일부터 달라졌습니다"],
                next_month_watch=["9월 국회"], tags=["부동산"])

    monkeypatch.setattr(monthly_mod, "ContentGenerator", FakeGenerator)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "테스트용-가짜-키")   # 호출은 FakeGenerator 가 받는다
    result = monthly_mod.run_monthly(cfg, month="2026-08", use_llm=True)
    assert result.llm_used and not result.skipped
    text = (result.out_dir / "monthly.md").read_text(encoding="utf-8")
    assert "2026년 6월 신고된 아파트 매매는 **2,252건**" in text   # 확정 달을 밝혀 쓴다
    assert "노원구 | 54.8%" in text and "흐름이 바뀐 지점" in text
    assert (result.out_dir / "monthly-naver.html").exists()


def test_sitemap_keeps_the_period_folder(cfg, tmp_path):
    from rebrief.site import _build_sitemap

    periods = [{"week": "2026-08", "dir": "monthly",
                "pages": [{"href": "monthly.html", "label": "결산 읽기"}]}]
    _build_sitemap(cfg, [], periods, tmp_path)
    xml = (tmp_path / "sitemap.xml").read_text(encoding="utf-8")
    assert "/monthly/2026-08/monthly.html" in xml


# ── 서울 지수 · 전세가율 지도 · 구 단위 급변 · 조용한 실패 · 용량 ────

def test_swings_name_the_neighbourhood_when_concentrated():
    from rebrief.stats import _busiest_dong, district_swings

    deals = [{"dong": "묵동"}] * 332 + [{"dong": "면목동"}] * 70 + [{"dong": "신내동"}] * 99
    spot = _busiest_dong(deals)
    assert spot["dong"] == "묵동" and spot["share"] == 66.3
    assert _busiest_dong([]) == {}

    counts = {"중랑구": 501, "동작구": 237, "종로구": 45, "강북구": 60}
    before = {"중랑구": 208, "동작구": 153, "종로구": 20, "강북구": 59}
    swings = district_swings(counts, before, {"중랑구": spot, "동작구": {"dong": "사당동", "share": 12.0}})
    names = [s["name"] for s in swings]
    assert names[:2] == ["중랑구", "동작구"]          # 변동 폭이 큰 순
    assert "종로구" not in names                      # 40건 미만은 비율이 튀므로 뺀다
    assert "강북구" not in names                      # 20% 미만은 '움직였다' 고 하지 않는다
    # 한 동네에 몰린 달은 그 동네를 밝힌다. 아니면 None (템플릿이 StrictUndefined 라 항목은 늘 있다)
    assert swings[0]["hotspot"]["dong"] == "묵동"
    assert swings[1]["hotspot"] is None


def test_empty_response_looks_like_a_crash_not_a_number():
    from rebrief.stats import suspect_drops

    rows = [
        {"name": "강남구", "was": {"count": 210}, "now": {"count": 3}},     # 호출 실패를 의심
        {"name": "노원구", "was": {"count": 673}, "now": {"count": 723}},   # 정상
        {"name": "중구", "was": {"count": 12}, "now": {"count": 0}},        # 원래 적은 곳은 말하지 않는다
    ]
    warns = suspect_drops(rows)
    assert len(warns) == 1 and warns[0].startswith("강남구")
    assert "응답이 비어 왔을 가능성" in warns[0]
    assert suspect_drops([]) == []


def test_jeonse_map_is_a_separate_metric():
    from rebrief import images

    names = [gu if gu.endswith("구") else f"{gu}구"
             for row in images.SEOUL_LAYOUT for gu in row.values()]
    data = {"month_label": "2026년 7월",
            "map": {n: 100 + i for i, n in enumerate(names)},
            "map_jeonse": {n: 38.8 + i for i, n in enumerate(names)}}

    count_img = images.district_choropleth(data, "2026-09-07")
    jeonse_img = images.district_choropleth(data, "2026-09-07", metric="jeonse")
    assert count_img.slug == "stats-map" and jeonse_img.slug == "stats-map-jeonse"
    assert "전세가율" in jeonse_img.title
    assert "38.8%" in jeonse_img.svg          # 칸에 %가 붙어야 한다 (건수 지도와 다른 형식)
    assert "갱신 계약은 뺐습니다" in jeonse_img.svg
    # 없는 지표를 달라고 하면 조용히 안 그린다
    assert images.district_choropleth(data, "2026-09-07", metric="없는것") is None
    # 전세가율은 짝이 없는 구가 빠지므로 절반만 차면 그리지 않는다
    thin = {"map_jeonse": {n: 40.0 for n in names[:9]}}
    assert images.district_choropleth(thin, "2026-09-07", metric="jeonse") is None


def test_png_replaces_the_svg(cfg, tmp_path, monkeypatch):
    from rebrief import images as images_mod
    from rebrief import render as render_mod
    from rebrief.render import Renderer

    out = tmp_path / "2026-09-07"
    out.mkdir()
    r = Renderer(cfg, out, "2026-09-07")
    img = images_mod.Image("demo", '<svg width="100" height="50"></svg>', "데모")

    made: list[int] = []

    def fake_png(svg_path, png_path, scale=2):
        made.append(scale)
        png_path.write_bytes(b"PNG")
        return True

    monkeypatch.setattr(render_mod.images_mod, "svg_to_png", fake_png)

    name = r._write_image(img, {"png": True, "png_scale": 2}, scale=1)
    assert name == "img-demo.png" and made == [1]
    assert not (out / "img-demo.svg").exists()          # 같은 그림을 두 벌 쌓지 않는다
    assert (out / "img-demo.svg") not in r.written

    # 변환에 실패한 날은 SVG 가 유일한 결과물이라 지우면 안 된다
    monkeypatch.setattr(render_mod.images_mod, "svg_to_png", lambda *a, **k: False)
    name = r._write_image(images_mod.Image("keep", '<svg width="10" height="10"></svg>', "유지"),
                          {"png": True})
    assert name == "img-keep.svg" and (out / "img-keep.svg").exists()


def test_storage_use_projects_a_year(cfg):
    from rebrief.site import _storage_use

    day = cfg.output_dir / "2026-09-07"
    day.mkdir(parents=True)
    (day / "big.png").write_bytes(b"x" * (2 * 1024 * 1024))
    use = _storage_use(cfg, 2)
    assert use["mb"] == 2.0 and use["per_day_mb"] == 1.0
    assert use["year_gb"] == 0.36                       # 하루 1MB × 365
    assert _storage_use(cfg, 0)["per_day_mb"] == 0


# ── 자치구 도식이 실제 위치를 지키는지 ──────────────────────

# 자치구청 기준 중심 좌표(위도, 경도). 도식은 실제 지형이 아니지만 **순서는 지켜야** 한다.
SEOUL_CENTER = {
    "도봉": (37.668, 127.032), "노원": (37.654, 127.075), "강북": (37.640, 127.011),
    "은평": (37.618, 126.928), "성북": (37.605, 127.018), "중랑": (37.598, 127.093),
    "종로": (37.595, 126.978), "서대문": (37.577, 126.937), "동대문": (37.575, 127.045),
    "마포": (37.560, 126.909), "중구": (37.560, 126.996), "강서": (37.556, 126.824),
    "성동": (37.550, 127.041), "강동": (37.549, 127.147), "광진": (37.538, 127.083),
    "용산": (37.532, 126.981), "양천": (37.524, 126.861), "영등포": (37.522, 126.910),
    "동작": (37.505, 126.943), "송파": (37.505, 127.115), "강남": (37.497, 127.063),
    "구로": (37.494, 126.858), "서초": (37.475, 127.032), "관악": (37.470, 126.947),
    "금천": (37.460, 126.898),
}


def test_district_grid_never_flips_north_or_east():
    """도식의 줄·칸이 실제 남북·동서 순서를 뒤집지 않아야 한다.

    예전 도식은 남북 11쌍·동서 4쌍이 뒤집혀 있었습니다 — 관악이 동작에서 아홉 칸 오른쪽에,
    광진·송파가 실제보다 다섯 자리 북쪽에 있었습니다. 칸을 옮길 일이 생기면 위 좌표를 보세요.
    """
    from rebrief.images import SEOUL_LAYOUT

    pos = {gu: (r, c) for r, row in enumerate(SEOUL_LAYOUT) for c, gu in row.items()}
    assert set(pos) == set(SEOUL_CENTER), "25개 구가 빠짐없이 한 번씩 있어야 합니다"

    flipped_ns = [(a, b) for a in pos for b in pos
                  if SEOUL_CENTER[a][0] > SEOUL_CENTER[b][0] and pos[a][0] > pos[b][0]]
    flipped_ew = [(a, b) for a in pos for b in pos
                  if SEOUL_CENTER[a][1] > SEOUL_CENTER[b][1] and pos[a][1] < pos[b][1]]
    assert not flipped_ns, f"북쪽 구가 아래 줄에 있습니다: {flipped_ns[:3]}"
    assert not flipped_ew, f"동쪽 구가 왼쪽 칸에 있습니다: {flipped_ew[:3]}"

    # 한 칸에 두 구를 넣으면 하나가 가려진다
    assert len(set(pos.values())) == 25


# ── 모델이 쓰는 글 줄이기 ────────────────────────────────────

def test_source_ids_become_urls(cfg):
    from types import SimpleNamespace

    from rebrief.models import DailyBrief, IssueBrief
    from rebrief.pipeline import RunResult, _fill_source_urls
    from rebrief.prompts import article_ids, format_clusters

    art = lambda n, url: SimpleNamespace(  # noqa: E731
        title=f"기사{n}", url=url, publisher="한국경제", feed_name="f",
        published=None, best_text="본문")
    cluster = SimpleNamespace(
        lead=art(1, "https://a.example/1"), size=2, publishers=["한국경제"],
        categories=["세금·절세"],
        articles=[art(1, "https://a.example/1"), art(2, "https://b.example/2")])
    clusters = [cluster]

    table = article_ids(clusters)
    assert table == {"1-1": "https://a.example/1", "1-2": "https://b.example/2"}
    text = format_clusters(clusters)
    assert "(1-1)" in text and "(1-2)" in text
    assert "https://" not in text            # 주소는 프롬프트에 넣지 않는다

    brief = DailyBrief(
        date="2026-09-07", headline="h", lead="l", market_temperature="m", tomorrow_watch=[],
        issues=[IssueBrief(title="t", one_liner="o", category="세금·절세", what_happened=["a"],
                           numbers=[], why_it_matters="w", who_is_affected=["x"], caution="없음",
                           source_ids=["1-2", "(1-1)", "9-9"])])
    result = RunResult(date="2026-09-07", out_dir=cfg.output_dir)
    _fill_source_urls(brief, clusters, result)
    assert brief.issues[0].source_urls == ["https://b.example/2", "https://a.example/1"]
    assert "1개를 알아보지 못했습니다" in result.warnings[0]   # 9-9 는 없는 번호

    # 번호를 하나도 못 받으면 이미 있는 주소를 지우지 않는다 (근거가 통째로 비면 검산도 빈다)
    keep = DailyBrief(
        date="2026-09-07", headline="h", lead="l", market_temperature="m", tomorrow_watch=[],
        issues=[IssueBrief(title="t", one_liner="o", category="세금·절세", what_happened=["a"],
                           numbers=[], why_it_matters="w", who_is_affected=["x"], caution="없음",
                           source_urls=["https://a.example/1"])])
    _fill_source_urls(keep, clusters, RunResult(date="2026-09-07", out_dir=cfg.output_dir))
    assert keep.issues[0].source_urls == ["https://a.example/1"]


def test_youtube_description_is_assembled_not_written():
    from rebrief.models import (DailyBrief, IssueBrief, LongformScript, LongformSection,
                                ShortsScript, VideoPack)
    from rebrief.render import pinned_comment, youtube_description

    brief = DailyBrief(
        date="2026-09-07", headline="종부세와 공급, 두 축", lead="오늘의 흐름입니다.",
        market_temperature="보합", tomorrow_watch=[],
        issues=[IssueBrief(title="종부세", one_liner="종부세가 22개 구로 번집니다",
                           category="세금·절세", what_happened=["a"], numbers=[],
                           why_it_matters="w", who_is_affected=["1주택자"], caution="없음",
                           source_urls=["https://a.example/1", "https://a.example/1"])])
    pack = VideoPack(
        shorts=ShortsScript(title_candidates=["t"], hook="h", lines=[], cta="c",
                            hashtags=["#부동산"], estimated_seconds=45),
        longform=LongformScript(
            title_candidates=["t1", "t2", "t3"], thumbnail_texts=["a", "b", "c"],
            cold_open="콜드오픈", outro="아웃트로", tags=["부동산"], estimated_minutes=8.0,
            sections=[LongformSection(chapter="종부세 확대", at="01:10", script="s",
                                      broll=["스톡: 아파트 항공"], graphics=["22개 구"])]))

    text = youtube_description(brief, pack, disclaimer="투자 판단은 본인 책임입니다.",
                               cta="구독 부탁드립니다.")
    assert "00:00 콜드오픈" in text and "01:10 종부세 확대" in text
    assert text.count("https://a.example/1") == 1        # 같은 주소는 한 번만
    assert "구독 부탁드립니다." in text and "투자 판단은 본인 책임입니다." in text

    assert pinned_comment(brief, "주의") == "· 종부세가 22개 구로 번집니다\n\n주의"
    # 모델은 더 이상 설명란·고정 댓글을 쓰지 않는다
    assert not hasattr(pack.longform, "description")
    assert not hasattr(pack.longform, "pinned_comment")


def test_brief_uses_its_own_model_and_no_cache(cfg, monkeypatch):
    import inspect

    from rebrief import llm as llm_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "테스트용-가짜-키")
    cfg.settings["llm"]["brief_model"] = "claude-sonnet-5"
    gen = llm_mod.ContentGenerator(cfg)
    assert gen.brief_model == "claude-sonnet-5" and gen.model != gen.brief_model

    # 캐시는 걷어냈다 — 구조화 출력 스키마가 접두사에 들어가 호출마다 새로 쓰이기만 했다
    src = inspect.getsource(llm_mod)
    assert "cache_control" not in src and "cache_system" not in src


# ── 품질 장부 · 쉬어 가는 날 · 시점 표시 · 정책↔실거래 · 결산 그림 ──

def test_quality_log_compares_before_and_after(tmp_path):
    from types import SimpleNamespace

    from rebrief.store import QualityLog

    book = QualityLog(tmp_path / "quality.json")
    check = lambda s: SimpleNamespace(status=s)  # noqa: E731
    for day, ok, unver in (("2026-09-01", 8, 0), ("2026-09-02", 8, 1),
                           ("2026-09-03", 6, 3), ("2026-09-04", 6, 3)):
        book.add(day, checklist={"ok": ok, "warn": 1, "fail": 0},
                 checks=[check("확인")] * 5 + [check("미확인")] * unver,
                 models=["claude-sonnet-5"], blog_chars=1800, issues=4, usd=0.3)
    book.save()

    again = QualityLog(tmp_path / "quality.json")
    assert len(again.recent()) == 4
    assert again.recent()[-1]["unverified"] == 3

    diff = again.compare(days=2)
    assert diff["days"] == 2 and diff["before_days"] == 2
    assert diff["ok"]["now"] == 6.0 and diff["ok"]["was"] == 8.0
    assert diff["ok"]["change"] == -2.0          # 값싼 모델로 내린 날 이렇게 드러난다
    assert diff["unverified"]["change"] == 2.5
    # 하루치뿐이면 견줄 게 없으니 아무 말도 하지 않는다
    assert QualityLog(tmp_path / "none.json").compare() == {}


def test_quiet_day_needs_a_story_several_outlets_carried(cfg):
    from types import SimpleNamespace

    from rebrief.rank import quiet_day

    cfg.settings["run"]["quiet_day"] = {"enabled": True, "min_top_size": 3, "min_covered": 2}
    small = [SimpleNamespace(size=1), SimpleNamespace(size=1)]
    busy = [SimpleNamespace(size=7), SimpleNamespace(size=1)]
    two = [SimpleNamespace(size=2), SimpleNamespace(size=2)]

    quiet, why = quiet_day(cfg, small, small)
    assert quiet and "여러 매체가 함께 다룬 이야기가 없습니다" in why
    assert quiet_day(cfg, busy, busy) == (False, "")
    assert quiet_day(cfg, two, two)[0] is False     # 2곳이 다룬 이슈가 둘이면 그냥 만든다
    assert quiet_day(cfg, [], [])[0] is True

    # 꺼 두면 어떤 날도 쉬지 않는다 (판정이 애매하면 만드는 쪽으로 기운다)
    cfg.settings["run"]["quiet_day"] = {"enabled": False}
    assert quiet_day(cfg, small, small) == (False, "")


def test_quiet_days_are_not_counted_as_failures(tmp_path):
    from datetime import date

    from rebrief.store import failure_streak

    out = tmp_path / "output"
    for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
        (out / day).mkdir(parents=True)
    (out / "2026-09-01" / "data.json").write_text("{}", encoding="utf-8")
    (out / "2026-09-03" / "quiet.json").write_text("{}", encoding="utf-8")   # 일부러 쉰 날

    # 9/4 실패 · 9/3 쉼(안 셈) · 9/2 실패 · 9/1 성공에서 멈춤
    assert failure_streak(out, date(2026, 9, 4)) == 2
    # 쉰 날만 있으면 연속 실패는 0 이다 — 조용한 이틀로 경보가 울리면 안 된다
    (out / "2026-09-04" / "quiet.json").write_text("{}", encoding="utf-8")
    (out / "2026-09-02" / "quiet.json").write_text("{}", encoding="utf-8")
    assert failure_streak(out, date(2026, 9, 4)) == 0


def test_articles_say_when_their_numbers_are_from():
    from rebrief.render import asof_block_html, asof_note

    plain = asof_note("2026-09-08")
    assert plain == "이 글은 2026년 9월 8일 기준으로 정리한 내용입니다."
    with_stats = asof_note("2026-09-08", {"month_label": "2026년 7월"})
    assert "실거래 수치는 2026년 7월 신고분입니다." in with_stats
    assert "2026년 9월 8일" in asof_block_html("2026-09-08")


def test_policy_docs_carry_the_district_numbers():
    from types import SimpleNamespace

    from rebrief.render import policy_region_links

    docs = [SimpleNamespace(news_id="1", title="노원구 일대 공공재개발 후보지 선정",
                            lead="", summary=["노원구가 후보지에 들었습니다."]),
            SimpleNamespace(news_id="2", title="항공 안전 대책", lead="", summary=[])]
    stats = {"month_label": "2026년 7월",
             "districts": [{"name": "노원구", "change": 50,
                            "now": {"count": 723, "avg": 700_000_000}}],
             "jeonse": [{"name": "노원구", "median": 54.8, "count": 128}]}
    links = policy_region_links(docs, stats)
    assert set(links) == {"1"}                    # 부동산과 무관한 발표에는 붙이지 않는다
    line = links["1"][0]
    assert "노원구" in line and "723건(+50건)" in line and "전세가율 54.8%" in line
    assert policy_region_links(docs, {}) == {}    # 통계가 없으면 조용히 넘어간다


def test_recaps_reuse_the_daily_stats_images(cfg, tmp_path):
    from rebrief.render import copy_stats_images

    day = cfg.output_dir / "2026-09-05"
    day.mkdir(parents=True)
    (day / "img-stats-map.png").write_bytes(b"PNG")
    (day / "img-stats-volume.png").write_bytes(b"PNG")

    out = tmp_path / "weekly"
    out.mkdir()
    made = copy_stats_images(cfg, out, "2026-09-07")     # 이틀 거슬러 올라가 찾는다
    assert made["map"] == "img-stats-map.png" and made["from"] == "2026-09-05"
    assert (out / "img-stats-map.png").exists()
    assert "jeonse_map" not in made                      # 없는 그림은 넣지 않는다
    assert copy_stats_images(cfg, out, "2026-08-01") == {}


# ── 공급 쪽 통계 (미분양·인허가·착공) ────────────────────────

def test_cumulative_permits_are_turned_back_into_months():
    """인허가 원자료는 연초부터의 누계다. 그대로 실으면 글이 거짓말이 된다."""
    from rebrief.stats import de_cumulate

    # 2026-09-08 실측 모양: 2025년 내내 쌓이다가 2026년 1월에 초기화된다
    rows = {"202511": 39299, "202512": 41912, "202601": 1250, "202602": 3856, "202603": 5715}
    got = de_cumulate(rows)
    assert got["202512"] == 41912 - 39299          # 그 달치 = 누계 차분
    assert got["202601"] == 1250                   # 1월은 누계가 곧 그 달치
    assert got["202602"] == 2606 and got["202603"] == 1859
    assert de_cumulate({}) == {}


def test_supply_tables_keep_their_own_kind(cfg, monkeypatch):
    from rebrief import stats as stats_mod

    cfg.settings["stats"]["supply"] = {
        "enabled": True, "months": 3,
        "tables": [
            {"name": "미분양", "id": "T1", "cls": "50018", "mode": "stock", "note": "서울 전체"},
            {"name": "인허가", "id": "T2", "cls": "50033", "mode": "cumulative"},
        ],
    }
    monkeypatch.setenv("REB_API_KEY", "테스트키")
    table = {
        "T1": {"202605": 985, "202606": 1013, "202607": 994},
        "T2": {"202604": 12890, "202605": 19208, "202606": 20838},
    }

    class Resp:
        def __init__(self, rows):
            self._rows = rows

        def raise_for_status(self):
            pass

        def json(self):
            return [{"head": []}, {"row": self._rows}]

    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if params.get("pIndex") != "1":            # 두 번째 쪽은 비어 온다
            return Resp([])
        return Resp([{"WRTTIME_IDTFR_ID": t, "DTA_VAL": v}
                     for t, v in table[params["STATBL_ID"]].items()])

    monkeypatch.setattr(stats_mod, "_get", fake_get)
    got = stats_mod.reb_supply(cfg, "2026-09-08")
    assert [g["name"] for g in got] == ["미분양", "인허가"]

    stock = got[0]
    assert stock["latest"] == 994 and stock["latest_label"] == "2026년 7월"
    assert stock["before"] == 1013 and stock["note"] == "서울 전체"

    permits = got[1]
    assert permits["latest"] == 20838 - 19208      # 누계를 그 달치로 되돌렸다
    assert [r["value"] for r in permits["rows"]][-2:] == [19208 - 12890, 20838 - 19208]

    # 키가 없으면 조용히 건너뛴다 (선택 기능이다)
    monkeypatch.delenv("REB_API_KEY")
    assert stats_mod.reb_supply(cfg, "2026-09-08") == []


def test_supply_chart_says_what_kind_of_number_it_is():
    from rebrief import images

    rows = [{"time": f"20260{i}", "label": f"2026년 {i}월", "value": 1000 + i * 10}
            for i in range(1, 8)]
    img = images.supply_line({"name": "미분양", "unit": "호", "mode": "stock",
                              "note": "서울 전체", "rows": rows}, "2026-09-07")
    assert img is not None and img.slug == "stats-supply"
    assert "재고" in img.svg and "1,070호" in img.svg

    permits = images.supply_line({"name": "인허가", "unit": "호", "mode": "cumulative",
                                  "rows": rows}, "2026-09-07")
    assert "누계라 그 달치로 되돌린 값" in permits.svg

    # 넉 달이 안 되면 추이라고 부를 수 없다
    assert images.supply_line({"name": "미분양", "rows": rows[:3]}, "2026-09-07") is None
    assert images.supply_line({}, "2026-09-07") is None


# ── 점검(감사)에서 찾아 고친 것들 ────────────────────────────


def test_blog_length_is_counted_the_way_the_prompt_asks():
    """분량은 **공백을 포함해** 센다. 프롬프트가 그렇게 부탁하기 때문이다.

    예전에는 공백을 빼고 세면서 여유도 ±20% 였다. 한국어 본문은 공백이 20%쯤이라
    두 실수가 정확히 상쇄돼, 목표의 3분의 2밖에 안 되는 글도 '통과'로 나왔다.
    """
    from rebrief import checklist as cl
    from rebrief.config import load_config
    from rebrief.models import BlogPost

    cfg = load_config()
    lo = int((cfg.get("blog", {}) or {}).get("min_chars", 1800))

    def verdict(text):
        items = cl.build(cfg, post=BlogPost(title="t", slug="s", meta_description="m",
                                            tags=["a"], body_markdown=text))
        return next(i for i in items if i.key == "blog_len")

    # 공백이 20%인, 목표에 딱 맞는 글 — 통과해야 한다
    body = ("가나다라 " * (lo // 5))[:lo]
    assert len(body) == lo and verdict(body).level == cl.OK

    # 목표의 4분의 3짜리 글 — 예전 셈법이면 놓쳤다
    short = body[: int(lo * 0.75)]
    assert verdict(short).level == cl.WARN
    assert f"{len(short):,}자" in verdict(short).title


def test_rerender_also_records_quality(cfg, monkeypatch):
    """`render` 로 다시 만든 날도 품질 장부에 남는다 (실행 경로에만 있었다)."""
    from rebrief import pipeline
    from rebrief.store import QualityLog
    from tests.test_pipeline import FakeGenerator

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)

    book = QualityLog(cfg.state_dir / "quality.json")
    book.days.pop("2026-09-06", None)          # 장부를 비우고 다시 만들어 본다
    book.save()

    pipeline.rerender(cfg, "2026-09-06", use_llm=True)
    again = QualityLog(cfg.state_dir / "quality.json")
    entry = again.days.get("2026-09-06", {})
    assert entry.get("ok", 0) >= 1 and entry.get("blog_chars", 0) > 0


def test_asof_note_speaks_month_and_week_like_a_person():
    """결산 글에는 '2026-W36' 이 아니라 '2026년 36주차' 라고 적힌다."""
    from rebrief.render import asof_note

    assert asof_note("2026-09-08").startswith("이 글은 2026년 9월 8일 기준")
    assert asof_note("2026-08").startswith("이 글은 2026년 8월 기준")
    assert asof_note("2026-W36").startswith("이 글은 2026년 36주차 기준")
    assert asof_note("") == ""                  # 날짜가 없으면 아무 말도 하지 않는다
    assert "2026년 7월" in asof_note("2026-09-08", {"month_label": "2026년 7월"})


def test_render_command_says_it_will_cost_money(cfg, monkeypatch, capsys):
    """`render` 는 이름과 달리 모델을 다시 부른다. 부르기 전에 얼마인지 말해 준다."""
    from types import SimpleNamespace

    from rebrief import cli
    from rebrief.llm import Usage
    from rebrief.store import CostLog

    book = CostLog(cfg.state_dir / "costs.json")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    book.record("2026-09-06", Usage(model="claude-opus-5", calls=3, input_tokens=1,
                                    output_tokens=1, _usd=0.4209))
    book.save()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    args = SimpleNamespace(date="2026-01-01", no_llm=False)
    assert cli._cmd_render(cfg, args) == 1            # 원본이 없어 실패하지만 경고는 이미 나왔다
    said = capsys.readouterr().out
    assert "모델을 다시 부릅니다" in said and "0.42달러" in said and "589원" in said
    assert "--no-llm" in said

    # --no-llm 은 돈이 들지 않으므로 아무 말도 하지 않는다
    cli._cmd_render(cfg, SimpleNamespace(date="2026-01-01", no_llm=True))
    assert "모델을 다시 부릅니다" not in capsys.readouterr().out


def test_quality_table_backfills_but_does_not_invent_numbers(cfg, tmp_path):
    """장부가 없던 날도 점검표에서 되살리되, 되살릴 수 없는 값은 0 인 척하지 않는다."""
    from rebrief.site import build_site
    from rebrief.store import QualityLog

    for day, ok in (("2026-09-05", 9), ("2026-09-06", 7)):
        out = cfg.output_dir / day
        out.mkdir(parents=True)
        (out / "brief.md").write_text("# b", encoding="utf-8")
        (out / "checklist.json").write_text(json.dumps(
            {"date": day, "summary": {"ok": ok, "warn": 1, "fail": 0}, "items": []}), encoding="utf-8")

    # 장부에 한 날만 제대로 들어 있다
    book = QualityLog(cfg.state_dir / "quality.json")
    book.add("2026-09-06", checklist={"ok": 7, "warn": 1, "fail": 0}, models=["claude-opus-5"],
             blog_chars=1850, issues=5, usd=0.42)
    book.save()

    html = (build_site(cfg, tmp_path / "site") / "dashboard.html").read_text(encoding="utf-8")
    assert "결과 품질" in html
    assert "1,850자" in html or "1850자" in html          # 장부가 있는 날은 그대로
    assert "기록 없음" in html                            # 되살린 날은 없다고 말한다

    # 되살린 날은 '무엇이 달라졌나' 평균에서 빠진다 (0 이 섞이면 거짓 개선이 된다)
    filled = QualityLog(cfg.state_dir / "quality.json")
    filled.backfill(cfg.output_dir)
    assert filled.days["2026-09-05"]["backfilled"] is True
    assert filled.days["2026-09-06"]["blog_chars"] == 1850   # 장부가 이긴다
    assert filled.compare(days=1) == {}                      # 견줄 진짜 날이 하나뿐


# ── 응답이 잘렸을 때 (2026-09-08 아침 실패) ──────────────────


def test_truncated_response_becomes_a_normal_error(cfg, monkeypatch):
    """한도에 걸려 잘린 응답은 LLMError 가 된다. 그대로 두면 파이프라인 밖까지 샌다.

    구조화 출력은 SDK 안에서 검사되므로 잘린 JSON 은 pydantic.ValidationError 로 납니다.
    anthropic 예외가 아니라서 아무도 잡지 않고, 2026-09-08 아침에는 그 탓에 **이미 만들어
    돈까지 낸 브리핑·블로그가 통째로 버려졌습니다.**
    """
    import pydantic

    from rebrief.llm import ContentGenerator, LLMError
    from rebrief.models import VideoPack

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    gen = ContentGenerator(cfg)

    def boom(**kwargs):
        raise pydantic.ValidationError.from_exception_data(
            "VideoPack", [{"type": "json_invalid", "loc": (), "input": "{...",
                           "ctx": {"error": "EOF while parsing a string at line 1 column 2728"}}])

    monkeypatch.setattr(gen.client.messages, "parse", boom)
    with pytest.raises(LLMError) as caught:
        gen._call("s", "u", VideoPack, "claude-sonnet-5", 16000)
    assert "잘렸습니다" in str(caught.value) and "16000" in str(caught.value)


def test_script_call_gets_its_own_bigger_budget(cfg, monkeypatch):
    """영상 대본만 한도를 크게 쓴다 — 출력이 가장 크고 생각 토큰까지 나눠 쓰기 때문."""
    from rebrief.llm import ContentGenerator
    from rebrief.models import DailyBrief

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    gen = ContentGenerator(cfg)
    assert gen.script_max_tokens > gen.max_tokens

    seen: list[int] = []

    def spy(**kwargs):
        seen.append(kwargs["max_tokens"])
        raise RuntimeError("여기까지만 본다")

    monkeypatch.setattr(gen.client.messages, "parse", spy)
    brief = DailyBrief(date="2026-09-08", headline="h", lead="l", issues=[],
                       market_temperature="m", tomorrow_watch=[])
    for call in (lambda: gen.generate_blog(brief), lambda: gen.generate_video(brief)):
        with pytest.raises(RuntimeError):
            call()
    assert seen == [gen.max_tokens, gen.script_max_tokens]   # 블로그는 기본, 대본은 큰 쪽


def test_run_survives_a_truncated_script(cfg, monkeypatch):
    """대본이 잘려도 브리핑·블로그는 남는다. 이미 돈을 낸 산출물을 버리지 않는다."""
    import pydantic

    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator

    class TruncatedScript(FakeGenerator):
        def generate_video(self, brief, stats=None):
            raise pydantic.ValidationError.from_exception_data(
                "VideoPack", [{"type": "json_invalid", "loc": (), "input": "{...",
                               "ctx": {"error": "EOF while parsing a string at line 1 column 2728"}}])

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", TruncatedScript)

    # 지금은 ValidationError 가 밖으로 새어 실행 전체가 죽는다 — 그러면 안 된다.
    result = pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    out = cfg.output_dir / "2026-09-06"
    assert (out / "brief.md").exists() and (out / "blog.md").exists()
    assert not (out / "script-longform.md").exists()
    assert any("영상 대본" in w for w in result.warnings)


# ── 카드뉴스 (유튜브 게시물용) ───────────────────────────────


def _brief_for_cards(issues: int = 5) -> dict:
    return {
        "headline": "분당 집값 1위·서울 월세 162만원",
        "market_temperature": "수도권 전반의 가격·임대료 상승 압력이 뚜렷하다.",
        "tomorrow_watch": ["은마아파트 공사비 협상", "주간 아파트 가격 동향 발표"],
        "issues": [
            {"title": f"이슈 {i}", "category": "가격동향", "one_liner": f"{i}번째 이야기입니다.",
             "what_happened": [f"사실 {i}-1 입니다.", f"사실 {i}-2 입니다."],
             "numbers": [{"value": f"{10 + i}", "unit": "%", "label": f"수치 {i}"}]}
            for i in range(1, issues + 1)
        ],
    }


def test_photo_search_picks_words_from_the_day(cfg):
    """찾을 말은 그날 이슈에서 나온다. 매일 같은 낱말이면 매일 같은 사진이 온다."""
    from rebrief import photos

    got = photos.queries_for({
        "headline": "은마아파트 재건축 사업시행인가",
        "issues": [{"title": "전세 보증금 반환 지연", "category": "임대차", "one_liner": ""},
                   {"title": "주담대 금리 인상", "category": "대출·금리", "one_liner": ""}],
    }, limit=3)
    assert len(got) == 3 and len(set(got)) == 3            # 세 장이 다 달라야 한다
    assert got[0] == photos.DEFAULT_QUERY                  # 표지는 그날 전체를 받는다
    assert any("window" in q for q in got)                 # 전세 → 아파트 외벽
    assert any("real estate" in q for q in got)            # 금리 → 부동산
    # 낱말마다 서울·한국이 들어가야 서양 주택 사진이 안 온다 (2026-09-08 실측)
    assert all("seoul" in q or "korea" in q for _, q in photos.QUERY_MAP)


def test_photo_search_survives_a_missing_key_and_a_dead_network(cfg, tmp_path, monkeypatch):
    """사진은 있으면 좋은 것이지 없으면 안 되는 것이 아니다. 무엇이 터져도 빈 목록."""
    from rebrief import photos

    brief = {"headline": "집값", "issues": []}
    kw = {"cache_dir": tmp_path / "c", "ledger": tmp_path / "l.json", "count": 2}
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    assert photos.fetch(brief, **kw) == []                 # 키가 없으면 조용히 건너뛴다

    def boom(*a, **k):
        raise OSError("망이 막혔습니다")

    monkeypatch.setattr(photos, "_get", boom)
    assert photos.fetch(brief, key="아무키", **kw) == []     # 망이 죽어도 실행은 산다


def test_photo_search_does_not_repeat_yesterdays_picture(cfg, tmp_path, monkeypatch):
    """어제 쓴 사진은 건너뛴다. 매일 같은 사진이 표지에 오르면 자동 생성 티가 난다."""
    import json

    from rebrief import photos

    ledger = tmp_path / "photos.json"
    ledger.write_text(json.dumps(["111"]), encoding="utf-8")

    def fake_get(url, headers, timeout=20):
        if url.startswith(photos.PEXELS_SEARCH):
            return json.dumps({"photos": [
                {"id": 111, "photographer": "어제 그 사람", "src": {"landscape": "http://x/1.jpg"}},
                {"id": 222, "photographer": "오늘 그 사람", "src": {"landscape": "http://x/2.jpg"}},
            ]}).encode()
        return b"\xff\xd8\xff\xe0jpeg"      # 사진 몸통 흉내

    monkeypatch.setattr(photos, "_get", fake_get)
    got = photos.fetch({"headline": "집값", "issues": []}, cache_dir=tmp_path / "c",
                       ledger=ledger, count=1, key="아무키")
    assert [p.ident for p in got] == ["222"]
    assert got[0].credit == "오늘 그 사람" and got[0].path.exists()
    assert json.loads(ledger.read_text()) == ["111", "222"]      # 장부에 쌓인다


def test_cards_credit_the_photo_and_say_it_is_unrelated(cfg, tmp_path):
    """사진에는 출처와 함께 '본문과 무관' 을 반드시 적는다.

    은마아파트 기사 옆에 아무 아파트 사진이 붙으면 읽는 사람은 그게 은마인 줄 압니다.
    """
    from rebrief import images

    art = tmp_path / "photo-222.jpg"
    _fake_png(art, 400, 224)      # 확장자만 jpg 인 가짜 파일이어도 띠 계산에는 문제없다
    got = images.cards(_brief_for_cards(), date="2026-09-08", channel="부동산 브리핑",
                       art=[(art, "사진 홍길동 / Pexels · 본문과 무관")])
    assert "본문과 무관" in got[0].svg and "홍길동" in got[0].svg


def _fake_png(path, width: int, height: int) -> None:
    """가로·세로만 맞는 진짜 PNG 한 장. 그림 라이브러리를 들이지 않으려고 직접 만든다."""
    import struct
    import zlib

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_card_art_band_skips_tall_graphics_but_keeps_photos(tmp_path):
    """카드 위 띠는 가득 채우므로 세로로 긴 인포그래픽은 축·범례가 잘린다. 그건 안 쓴다.

    사람이 넣어 둔 사진(`photo-*`)은 잘려도 되므로 비율을 보지 않는다.
    """
    from rebrief import images

    wide = tmp_path / "img-1-stat-card.png"
    tall = tmp_path / "img-stats-map.png"
    photo = tmp_path / "photo-1.png"
    _fake_png(wide, 200, 112)          # 1.79 — 표·수치 카드
    _fake_png(tall, 112, 122)          # 0.92 — 자치구 지도
    _fake_png(photo, 112, 122)         # 세로지만 사진이라 그대로 쓴다

    assert images._art_band(wide) is not None
    assert images._art_band(tall) is None
    assert images._art_band(photo) is not None
    assert images._art_band(tmp_path / "없는파일.png") is None


def test_cards_put_the_graphic_on_top_and_the_words_below(cfg, tmp_path):
    """위쪽은 그림, 아래쪽은 글. 그림이 있어도 본문 한 줄은 반드시 남는다.

    처음 만들었을 때 띠를 548px 로 잡았더니 아래 판이 348px 밖에 안 남아 이슈 카드의
    한 줄 요약이 통째로 잘려 나갔습니다. 그래서 여기서 재서 지킵니다.
    """
    from rebrief import images

    art = tmp_path / "img-1-stat-card.png"
    _fake_png(art, 400, 224)
    got = images.cards(_brief_for_cards(), date="2026-09-08", channel="부동산 브리핑",
                       art=[art])
    cover = got[0].svg
    assert "base64," in cover and "preserveAspectRatio=\"xMidYMid slice\"" in cover
    assert f'height="{images.CARD_BAND}"' in cover
    # 그림은 표지에 한 장만 들어갔고 나머지는 글자 카드로 남는다
    assert sum(1 for g in got if "<image" in g.svg) == 1
    issue = next(g for g in got if g.slug.endswith("-issue"))
    assert "번째 이야기입니다." in issue.svg


def test_number_card_drops_a_number_instead_of_overflowing(cfg):
    """수치가 다 안 들어가면 카드 밖으로 흘리지 말고 덜어 낸다.

    2026-09-08 에 작은 글자를 1.5배로 키웠더니 세 번째 설명이 쪽번호와 겹친 채
    카드 아래로 흘러나갔습니다. 줄이기보다 덜어 냅니다 — '오늘의 숫자' 카드에서
    숫자가 가장 작아지면 앞뒤가 안 맞습니다.
    """
    from rebrief import images

    긴설명 = "서울 정책대출(주금공) 대상 아파트 가격 기준으로 본 최근 상황"
    brief = _brief_for_cards()
    brief["issues"][0]["numbers"] = [{"value": "6", "unit": "억원 이하", "label": 긴설명}]
    got = images.cards(brief, date="2026-09-08",
                       key_numbers=[{"value": "6", "unit": "억원 이하", "label": 긴설명},
                                    {"value": "29.5", "unit": "%", "label": 긴설명},
                                    {"value": "28.4", "unit": "%", "label": 긴설명}])
    card = next(g for g in got if g.slug.endswith("-numbers"))
    ys = [float(m) for m in re.findall(r'<text x="96" y="([0-9.]+)"', card.svg)]
    assert ys and max(ys) < 1080 - 100, f"글이 카드 밖으로 나갔다 (y={max(ys):.0f})"
    # 설명이 두 줄로 풀릴 만큼 길면 수치를 덜어 낸다 — 말을 잘라 박지 않는다.
    assert 1 <= card.svg.count(f'font-family="{images.DISPLAY}"') <= 3
    말 = "".join(re.findall(r'font-size="3[0-9]" fill="[^"]*">([^<]*)</text>', card.svg))
    assert 말.endswith("최근 상황"), f"설명이 잘렸다: {말[-20:]!r}"


def test_number_card_fits_three_when_the_labels_are_normal_length(cfg):
    """설명이 보통 길이면 세 수치가 다 올라간다.

    설명을 **한 줄로 놓고 안쪽 테두리까지 넓게** 쓴 덕입니다 (2026-09-08 사용자 제안).
    두 줄을 허용하면 한 줄당 60px 씩 먹어 셋째가 밀려납니다.
    """
    from rebrief import images

    keys = [{"value": "6", "unit": "억원 이하", "label": "서울 정책대출 대상 아파트 가격 기준"},
            {"value": "29.5", "unit": "%", "label": "최근 1년 아파트값 상승률 1위(분당구)"},
            {"value": "28.4", "unit": "%", "label": "광명시 아파트값 상승률"}]
    got = images.cards(_brief_for_cards(), date="2026-09-08", key_numbers=keys)
    card = next(g for g in got if g.slug.endswith("-numbers"))
    assert card.svg.count(f'font-family="{images.DISPLAY}"') == 3


def test_page_number_stays_inside_the_inner_border():
    """쪽번호가 안쪽 테두리 위에 걸치면 안 된다.

    테두리 아래 변이 h-58 인데 쪽번호를 h-60 에 두어 글자 아랫부분이 선과 겹쳤습니다
    (2026-09-08). 날짜가 위 테두리에서 떨어진 만큼 띄웁니다.
    """
    from rebrief import images

    svg = images.cards(_brief_for_cards(), date="2026-09-08", channel="부동산 브리핑")[1].svg
    page = re.search(r'<text x="\{?[0-9w-]*\}?[^"]*" y="([0-9]+)"[^>]*>\d\d / \d\d</text>', svg)
    page = page or re.search(r'y="([0-9]+)"[^>]*>\d\d / \d\d</text>', svg)
    assert page, "쪽번호를 찾지 못했습니다"
    assert float(page.group(1)) + 8 < 1080 - 58, "쪽번호가 안쪽 테두리와 겹칩니다"


def test_cards_are_square_and_numbered(cfg):
    from rebrief import images

    got = images.cards(_brief_for_cards(), date="2026-09-08", channel="부동산 브리핑")
    assert 5 <= len(got) <= 7                       # 유튜브 게시물 한 묶음
    assert got[0].slug == "card-1-cover"
    for img in got:
        assert 'width="1080" height="1080"' in img.svg      # 정사각 — 세로는 잘리는 화면이 있다
        assert f"/ {len(got):02d}" in img.svg               # 쪽번호(02 / 07)가 실제 장수와 맞는다
    assert "오늘의 숫자" in got[1].svg
    assert "내일 볼 것" in got[-1].svg

    # 자료가 적은 날은 억지로 채우지 않고 줄어든다
    small = images.cards({"headline": "조용한 하루", "issues": [
        {"title": "하나", "one_liner": "한 줄", "numbers": []}]}, date="2026-09-08")
    assert 0 < len(small) < 5
    assert "01 / " not in small[0].svg or len(small) > 1     # 한 장이면 쪽번호를 찍지 않는다


def test_cards_badge_only_uses_a_number_that_is_in_the_headline():
    """'6' 이 '162만원' 안의 6 에 걸려 엉뚱한 배지가 달렸다 (2026-09-08)."""
    from rebrief import images

    brief = _brief_for_cards()
    brief["issues"][0]["numbers"] = [{"value": "6", "unit": "억원 이하", "label": "대출 기준"}]
    cover = images.cards(brief, date="2026-09-08")[0].svg
    assert "억원 이하" not in cover          # 제목에 없는 수치는 배지로 달지 않는다

    brief["issues"][0]["numbers"] = [{"value": "162", "unit": "만원", "label": "평균 월세"}]
    assert "162만원" in images.cards(brief, date="2026-09-08")[0].svg


def test_cards_do_not_call_the_model(cfg, monkeypatch):
    """카드뉴스는 이미 만든 브리핑을 나눠 담을 뿐이라 하루 비용이 늘지 않는다."""
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator

    calls = {"n": 0}

    class Counting(FakeGenerator):
        def _count(self):
            calls["n"] += 1

        def generate_brief(self, *a, **k):
            self._count(); return super().generate_brief(*a, **k)

        def generate_blog(self, *a, **k):
            self._count(); return super().generate_blog(*a, **k)

        def generate_video(self, *a, **k):
            self._count(); return super().generate_video(*a, **k)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", Counting)
    cfg.settings["images"]["enabled"] = True
    cfg.settings["images"]["cards"] = True
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)

    assert calls["n"] == 3                       # 브리핑·블로그·대본. 카드 때문에 늘지 않았다
    made = sorted(p.name for p in (cfg.output_dir / "2026-09-06").glob("card-*.svg"))
    assert made and made[0].startswith("card-1-cover")


def test_card_palette_stays_readable():
    """카드 색은 눈이 아니라 대비로 정한다.

    색을 고를 때마다 실제로 걸렸다 — 주황 판에서 두 곳(2.78·2.75), 청사진 판에서 또 두 곳
    (짙은 낯의 낮은 글씨 4.29, 밝은 낯의 낮은 글씨 3.72). 모두 작은 글씨라 4.5 를 넘겨야 한다.
    눈으로 보면 "괜찮아 보이는" 값들이라 여기서 재서 지킨다.
    """
    from rebrief import images

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    faces = {"남색": images.CARD_DARK, "짙은 남색": images.CARD_FLOOD,
             "밝은 청사진": images.CARD_PAPER}
    for face_name, (ground, ink, dim, _rule, signal) in faces.items():
        for role, color in (("글씨", ink), ("낮은 글씨", dim), ("신호색", signal)):
            got = ratio(color, ground)
            assert got >= 4.5, f"{face_name} 위 {role} 대비 {got:.2f}"


def test_cards_embed_only_the_letters_they_use():
    """글꼴을 SVG 에 심되 **쓴 글자만** 잘라 심는다 — 러너에는 이 글꼴이 없다.

    통째로 심으면 프리텐다드 두 굵기만 3MB 라 카드 한 장이 4MB 를 넘는다.
    실제로 쓰는 글자는 200자 안쪽이라 잘라내면 수십 KB 로 줄어든다.
    """
    from rebrief import images

    css = images._font_css("분당 집값 29.5%")
    for family in ("IBMPlexMono", "IBMPlexMonoBold", "Pretendard", "PretendardBold"):
        assert f"'{family}'" in css
    assert "base64," in css
    assert len(css) < 400_000, f"글꼴이 너무 크다 — {len(css)//1024}KB"

    cover = images.cards(_brief_for_cards(), date="2026-09-08")[0].svg
    assert "@font-face" in cover and images._FONT_CSS_TOKEN not in cover
