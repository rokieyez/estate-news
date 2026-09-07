"""9월 7일에 추가한 것들 — 비용 분리, 자막·컷 리스트, 선정 근거, 반복 감지, 예정 페이지, 발행 기록."""

from __future__ import annotations

import json

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
