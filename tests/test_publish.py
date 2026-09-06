"""3차 — 점검표, 월 예산 가드, 어제와 달라진 점, 지난 글 검색, 썸네일 2안."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

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
