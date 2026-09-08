"""3차 — 점검표, 월 예산 가드, 어제와 달라진 점, 지난 글 검색, 썸네일 2안."""

from __future__ import annotations

import json
from datetime import date

import pytest

from rebrief import checklist as cl
from rebrief.store import CostLog
from rebrief.llm import Usage


# ── 점검표 ───────────────────────────────────────────────────


def test_LLM_이_없으면_막힘_하나만(cfg):
    items = cl.build(cfg, llm_used=False)
    assert [i.level for i in items] == [cl.FAIL]


def test_금지_표현을_실제로_잡는다(cfg):
    from tests.test_pipeline import make_pack, make_post
    post = make_post()
    post.body_markdown += "\n\n지금 안 사면 늦는다는 말이 돕니다."
    items = cl.build(cfg, post=post, pack=make_pack(), llm_used=True)
    banned = next(i for i in items if i.key == "banned")
    assert banned.level == cl.FAIL and banned.lines == ["블로그: '지금 안 사면 늦는다'"]


def test_깨끗한_산출물은_통과한다(cfg):
    from tests.test_pipeline import make_pack, make_post
    cfg.settings["blog"]["min_chars"] = 10
    items = cl.build(cfg, post=make_post(), pack=make_pack(), llm_used=True)
    by = {i.key: i for i in items}
    assert by["banned"].level == cl.OK and by["blog_len"].level == cl.OK
    assert cl.summarize(items)[cl.FAIL] == 0


def test_점검표가_파일과_사이트_카드로_나온다(cfg, monkeypatch, tmp_path):
    from rebrief import pipeline
    from rebrief.site import build_site
    from tests.test_pipeline import FakeGenerator
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    cfg.settings["blog"]["min_chars"] = 10
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    out = cfg.output_dir / "2026-09-06"
    assert (out / "checklist.md").exists()
    data = json.loads((out / "checklist.json").read_text(encoding="utf-8"))
    assert data["summary"]["fail"] == 0 and any(i["key"] == "photos" for i in data["items"]) is False
    dest = build_site(cfg, tmp_path / "site")
    index = (dest / "index.html").read_text(encoding="utf-8")
    assert "발행 전 점검" in index and (dest / "latest" / "checklist.html").exists()


def test_LLM_없는_실행도_점검표는_남는다(cfg, monkeypatch):
    from rebrief import pipeline
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pipeline.run(cfg, run_date="2026-09-06", use_llm=False)
    data = json.loads((cfg.output_dir / "2026-09-06" / "checklist.json").read_text(encoding="utf-8"))
    assert data["summary"]["fail"] == 1


# ── 월 예산 가드 ─────────────────────────────────────────────


def _spend(cfg, usd, day=None):
    day = day or date.today().isoformat()
    u = Usage(model="claude-opus-5"); u.calls, u.input_tokens = 1, int(usd / 5.0 * 1_000_000)   # $5/M 입력
    log = CostLog(cfg.state_dir / "costs.json"); log.record(day, u); log.save()


def test_예산의_80퍼센트를_넘으면_절약_모델(cfg):
    from rebrief.pipeline import RunResult, _budget_guard
    cfg.settings["llm"]["monthly_budget_usd"] = 10
    cfg.settings["llm"]["fallback_model"] = "claude-sonnet-5"
    _spend(cfg, 8.5)
    r = RunResult(date="x", out_dir=cfg.output_dir)
    assert _budget_guard(cfg, r) == "claude-sonnet-5" and "80%" in r.warnings[0]


def test_예산을_넘으면_건너뛴다(cfg):
    from rebrief.pipeline import RunResult, _budget_guard
    cfg.settings["llm"]["monthly_budget_usd"] = 10
    _spend(cfg, 10.5)
    r = RunResult(date="x", out_dir=cfg.output_dir)
    assert _budget_guard(cfg, r) == "" and "예산" in r.warnings[0]


def test_예산이_0이면_제한_없음(cfg):
    from rebrief.pipeline import RunResult, _budget_guard
    cfg.settings["llm"]["monthly_budget_usd"] = 0
    _spend(cfg, 999)
    assert _budget_guard(cfg, RunResult(date="x", out_dir=cfg.output_dir)) is None


def test_이번_달_합계는_지난달을_빼고_센다(tmp_path):
    log = CostLog(tmp_path / "c.json")
    u = Usage(model="claude-opus-5"); u.calls, u.input_tokens = 1, 1_000_000
    log.record("2026-08-31", u); log.record("2026-09-01", u); log.record("2026-09-06", u)
    assert log.this_month(date(2026, 9, 6)) == pytest.approx(10.0)


# ── 어제와 달라진 점 ─────────────────────────────────────────


def test_어제와_비교해_새것_이어지는것_빠진것을_나눈다(cfg):
    from rebrief.pipeline import _diff_yesterday
    from tests.test_pipeline import make_brief
    prev = cfg.output_dir / "2026-09-05"; prev.mkdir(parents=True)
    (prev / "data.json").write_text(json.dumps({"issues": [
        {"title": "서울 아파트값 2주 연속 하락"}, {"title": "금리 동결"}]}, ensure_ascii=False), encoding="utf-8")
    diff = _diff_yesterday(cfg, make_brief(), "2026-09-06")
    assert diff["date"] == "2026-09-05"
    assert diff["continuing"] == ["서울 아파트값 3주 연속 하락"]
    assert diff["new"] == ["전세사기 지원 확대"] and diff["gone"] == ["금리 동결"]


def test_이전_날짜가_없으면_비교하지_않는다(cfg):
    from rebrief.pipeline import _diff_yesterday
    from tests.test_pipeline import make_brief
    assert _diff_yesterday(cfg, make_brief(), "2026-09-06") == {}


def test_브리핑에_달라진_점이_실린다(cfg, monkeypatch):
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    prev = cfg.output_dir / "2026-09-05"; prev.mkdir(parents=True)
    (prev / "data.json").write_text(json.dumps({"issues": [{"title": "금리 동결"}]}), encoding="utf-8")
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    text = (cfg.output_dir / "2026-09-06" / "brief.md").read_text(encoding="utf-8")
    assert "2026-09-05 와 달라진 점" in text and "⏹ 금리 동결" in text


# ── 지난 글 검색 ─────────────────────────────────────────────


def test_검색_색인과_페이지(cfg, tmp_path):
    from rebrief.site import build_site
    for d, title in (("2026-09-05", "금리 동결"), ("2026-09-06", "종부세 확산")):
        day = cfg.output_dir / d; day.mkdir(parents=True)
        (day / "brief.md").write_text("# b", encoding="utf-8")
        (day / "data.json").write_text(json.dumps({"headline": title, "issues": [
            {"title": title, "category": "정책", "one_liner": "x", "numbers": [{"label": "값", "value": "1", "unit": "%"}]}]},
            ensure_ascii=False), encoding="utf-8")
    dest = build_site(cfg, tmp_path / "site")
    index = json.loads((dest / "search-index.json").read_text(encoding="utf-8"))
    assert [d["date"] for d in index] == ["2026-09-06", "2026-09-05"]
    page = (dest / "search.html").read_text(encoding="utf-8")
    assert "종부세 확산" in page and "search.html" in (dest / "index.html").read_text(encoding="utf-8")


def test_색인의_닫는_스크립트_태그는_무력화된다(cfg, tmp_path):
    from rebrief.site import build_site
    day = cfg.output_dir / "2026-09-06"; day.mkdir(parents=True)
    (day / "brief.md").write_text("# b", encoding="utf-8")
    (day / "data.json").write_text(json.dumps({"issues": [{"title": "</script><b>x", "numbers": []}]}), encoding="utf-8")
    dest = build_site(cfg, tmp_path / "site")
    page = (dest / "search.html").read_text(encoding="utf-8")
    assert "</script><b>x" not in page and "<\\/script>" in page


# ── 썸네일 2안 ───────────────────────────────────────────────


def test_썸네일은_후보_두_개를_그린다(cfg, tmp_path):
    from rebrief.render import Renderer
    from tests.test_pipeline import make_pack
    r = Renderer(cfg, tmp_path / "out", "2026-09-06")
    r.thumbnails(make_pack())
    names = sorted(p.name for p in (tmp_path / "out").glob("thumb-*.svg"))
    assert names == ["thumb-longform-2.svg", "thumb-longform.svg", "thumb-shorts-2.svg", "thumb-shorts.svg"]


# ── 수치 강조: 되풀이 형광펜 · 핵심 수치 · 오늘의 숫자 카드 ───


def test_repeated_numbers_highlight_only_the_first_time():
    from rebrief.render import to_naver_html

    body = (
        "## 상승률 10% 전망\n\n"
        "집값 상승률이 10%를 넘었다. 상승률 10%인 집값. 집값 상승률 10%를 잡기 위해. "
        "2026년, 2026년, 2026년 9월 6일. 거래가 3억 원·3억원·3억원. 5억원은 5억원 두 번.\n\n"
        "[이미지: 10% 상승률 그래프]"
    )
    html = to_naver_html(body, {1: "img-1-stat-card.png"})
    # 첫 등장만 형광펜. 두 번째부터는 아무 표시도 하지 않는다 — 예전에는 밑줄을 그었는데
    # 5천 자 글에 강조가 42회나 쌓여 무엇이 중요한지 알 수 없었다 (2026-09-08).
    assert html.count(">10%</span>") == 1 and "<u>" not in html
    assert "<h2>상승률 10% 전망</h2>" in html          # 소제목은 건드리지 않는다
    assert "집값</span>" not in html and "2026년</span>" not in html   # 주제어·연도는 제외
    assert ">3억 원</span>" in html and html.count(">3억원</span>") == 0  # 띄어쓰기 달라도 같은 수치
    assert "5억원</span>" not in html
    assert 'alt="10% 상승률 그래프"' in html
    assert "<span" not in to_naver_html(body, highlight_min=0)


def test_key_numbers_bold_and_card():
    from rebrief.keynumbers import KeyNumber
    from rebrief.render import to_naver_html

    keys = [KeyNumber("11%", "11%", "서울 집값 연간 상승 가정", "2030년까지"),
            KeyNumber("22개구", "22개구", "종부세 대상 자치구")]
    html = to_naver_html("집값이 매년 11% 오르면 22개구가 대상. 다시 11%.", key_numbers=keys)
    assert '<span class="hl" style="background-color:#fff59d"><strong>11%</strong></span>' in html   # 한 번만 나와도 핵심이면 강조
    assert "<strong>22개구</strong>" in html
    assert html.count('class="hl"') == 2 and "<u>" not in html   # 11%·22개구 각 한 번씩, 두 번째 11% 는 맨몸
    # '오늘의 숫자' 카드는 뺐다 — 3줄 요약과 같은 수치를 한 번 더 말하고 있었다 (2026-09-08).
    assert "오늘의 숫자" not in html


def test_pick_prefers_numbers_used_in_body():
    from rebrief.keynumbers import pick
    from rebrief.models import DailyBrief, DataPoint, IssueBrief

    def issue(title, nums):
        return IssueBrief(title=title, one_liner="x", category="가격동향", what_happened=["a"],
                          numbers=nums, why_it_matters="y", who_is_affected=[], caution="없음", source_urls=[])

    brief = DailyBrief(date="2026-09-07", headline="h", lead="l", market_temperature="t", tomorrow_watch=[], issues=[
        issue("첫째", [DataPoint(label="연도", value="2030", unit="년"), DataPoint(label="상승률", value="11", unit="%"),
                      DataPoint(label="자치구", value="22", unit="개구")]),
        issue("둘째", [DataPoint(label="감소율", value="81", unit="%", period="3년새")]),
        issue("셋째", [DataPoint(label="본문에 없는 수치", value="4", unit="조원")]),
    ])
    got = pick(brief, "집값이 11% 오르면 22개구. 임대주택 81% 감소.", 3)
    assert [k.display for k in got] == ["11%", "81%", "22개구"]   # 이슈당 하나 먼저, 본문에 있는 것 먼저
    assert got[1].note == "3년새"
    assert pick(brief, "", 0) == [] and pick(None, "x") == []


def test_thumbnail_badge():
    from rebrief import images

    svg = images.thumbnail("서울 22개구 종부세?", badge="11%", size=(1080, 1920)).svg
    assert ">11%</text>" in svg
    assert ">11%" not in images.thumbnail("제목").svg


def test_renderer_reads_highlight_threshold(cfg, tmp_path):
    from rebrief.render import Renderer
    from tests.test_pipeline import make_post

    post = make_post()
    post.body_markdown = "값 7%. 또 7%. 다시 7%."
    cfg.settings["blog"]["highlight_repeats"] = 0
    off = Renderer(cfg, tmp_path / "off", "2026-09-06").blog_naver(post).read_text(encoding="utf-8")
    cfg.settings["blog"]["highlight_repeats"] = 3
    on = Renderer(cfg, tmp_path / "on", "2026-09-06").blog_naver(post).read_text(encoding="utf-8")
    assert "7%</span>" not in off.split('id="post"')[1]
    assert on.count('class="hl"') == 1 and "<u>" not in on   # 첫 등장만 형광펜, 나머지는 맨몸


# ── HTML 안전장치 ──────────────────────────────────────────


def test_clean_html_strips_scripts_but_keeps_formatting():
    from rebrief.sanitize import clean_html

    dirty = ('<h2>제목</h2><script>alert(1)</script><p onclick="x()">본문 <b>굵게</b></p>'
             '<a href="javascript:alert(1)">링크</a><img src="x" onerror="alert(1)"><table><tr><td>1</td></tr></table>')
    out = clean_html(dirty)
    assert "<script" not in out and "onclick" not in out and "onerror" not in out and "javascript:" not in out
    assert "<h2>제목</h2>" in out and "<b>굵게</b>" in out and "<td>1</td>" in out


def test_html_templates_escape_model_text_but_not_body(cfg, tmp_path):
    from rebrief.render import Renderer
    from tests.test_pipeline import make_post

    post = make_post()
    post.title = '제목 <script>alert("x")</script>'
    post.body_markdown = "## 소제목\n\n<script>alert(1)</script>본문 **굵게**"
    html = Renderer(cfg, tmp_path, "2026-09-07").blog_naver(post).read_text(encoding="utf-8")
    assert "<script>alert" not in html                 # 제목은 이스케이프, 본문은 정화
    assert "&lt;script&gt;" in html                     # 제목의 태그가 글자로 보인다
    assert "<h2>소제목</h2>" in html and "<strong>굵게</strong>" in html   # 본문 HTML 은 살아 있다


def test_collect_summary_written_without_article_text(tmp_path):
    import json
    from rebrief.models import Article
    from rebrief.store import save_raw

    a = Article(id="a1", title="t", url="https://x.test/1", feed_id="f", feed_name="F", published="2026-09-07T00:00:00+00:00", summary="s", body="비밀 본문")
    save_raw(tmp_path / "raw" / "articles.json", [a], {"feeds": [{"id": "f", "ok": True}]})
    summary = json.loads((tmp_path / "collect.json").read_text(encoding="utf-8"))
    assert summary["articles"] == 1 and summary["feeds"][0]["ok"] is True
    assert "비밀 본문" not in (tmp_path / "collect.json").read_text(encoding="utf-8")
