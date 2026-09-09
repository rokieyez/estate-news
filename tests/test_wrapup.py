"""4차 — 원문 보기, 금지 표현 자동 수정, 연속 실패, 주간 추이 그림, PWA."""

from __future__ import annotations

import json
from datetime import date

from rebrief import checklist as cl
from rebrief import notify, verify
from rebrief.models import Article, Cluster, DailyBrief, DataPoint, IssueBrief
from rebrief.store import SeriesStore, failure_streak


def _cluster(url, body):
    return Cluster(key=url, articles=[Article(id="x", title="t", url=url, feed_id="f", feed_name="n", body=body)])


def _brief(value, unit, urls):
    return DailyBrief(date="d", headline="h", lead="l", market_temperature="m", tomorrow_watch=[],
                      issues=[IssueBrief(title="i", one_liner="o", category="c", what_happened=["w"],
                                         numbers=[DataPoint(label="라벨", value=value, unit=unit)],
                                         why_it_matters="y", who_is_affected=[], caution="없음", source_urls=urls)])


# ── 원문 보기 ────────────────────────────────────────────────


def test_확인된_수치는_기사_링크와_문장을_가진다():
    body = "앞 문장이다. 서울 22개구가 종부세 대상이 된다는 분석이다. 뒤 문장이다. " * 10
    [c] = verify.check_numbers(_brief("22", "개구", ["https://a/1"]), [_cluster("https://a/1", body)])
    assert c.status == verify.VERIFIED and c.url == "https://a/1"
    assert "22개구" in c.snippet and "앞문장" not in c.snippet          # 그 문장만


def test_브리핑에_원문_보기가_실린다(cfg, monkeypatch):
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    text = (cfg.output_dir / "2026-09-06" / "brief.md").read_text(encoding="utf-8")
    assert "## 숫자 검산" in text


# ── 금지 표현 자동 수정 ──────────────────────────────────────


def test_금지_표현이_든_문장만_골라낸다():
    text = "집값이 올랐습니다. 지금 안 사면 늦는다는 말이 돕니다. 신중하세요."
    assert cl.sentences_with(text, ["지금 안 사면 늦는다"]) == [" 지금 안 사면 늦는다는 말이 돕니다."]


def test_자동_수정은_문장을_바꾸고_전후를_남긴다():
    text = "집값이 올랐습니다. 지금 안 사면 늦는다는 말이 돕니다."
    new, changes = cl.autofix(text, ["지금 안 사면 늦는다"], lambda s, p: "매수를 서두르라는 말이 돕니다.")
    assert new == "집값이 올랐습니다. 매수를 서두르라는 말이 돕니다."
    assert changes == [("지금 안 사면 늦는다는 말이 돕니다.", "매수를 서두르라는 말이 돕니다.")]


def test_고친_문장에_금지_표현이_남으면_바꾸지_않는다():
    text = "지금 안 사면 늦는다."
    new, changes = cl.autofix(text, ["지금 안 사면 늦는다"], lambda s, p: "정말 지금 안 사면 늦는다.")
    assert new == text and changes == []


def test_고쳐_쓰기가_터져도_본문은_그대로다():
    def boom(s, p):
        raise RuntimeError("x")
    new, changes = cl.autofix("지금 안 사면 늦는다.", ["지금 안 사면 늦는다"], boom)
    assert new == "지금 안 사면 늦는다." and changes == []


def test_파이프라인이_금지_표현을_고쳐_쓰고_점검표에_적는다(cfg, monkeypatch):
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator, make_post

    class Fixing(FakeGenerator):
        def generate_blog(self, brief):
            post = make_post()
            post.body_markdown += "\n\n지금 안 사면 늦는다는 분위기입니다."
            return post

        def rewrite(self, sentence, phrases, tone=""):
            self.usage.calls += 1
            return "서두르는 분위기입니다."

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", Fixing)
    cfg.settings.setdefault("checklist", {})["autofix_model"] = "claude-haiku-4-5"
    result = pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    out = cfg.output_dir / "2026-09-06"
    blog = (out / "blog.md").read_text(encoding="utf-8")
    assert "지금 안 사면" not in blog and "서두르는 분위기입니다." in blog
    data = json.loads((out / "checklist.json").read_text(encoding="utf-8"))
    assert data["summary"]["fail"] == 0
    assert data["items"][0]["key"] == "autofix" and "→ 서두르는" in data["items"][0]["lines"][0]
    assert any("고쳐 썼습니다" in w for w in result.warnings)


def test_자동_수정_모델이_비어_있으면_손대지_않는다(cfg, monkeypatch):
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator, make_post

    class Dirty(FakeGenerator):
        def generate_blog(self, brief):
            post = make_post()
            post.body_markdown += "\n\n지금 안 사면 늦는다."
            return post

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", Dirty)
    cfg.settings.setdefault("checklist", {})["autofix_model"] = ""
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    data = json.loads((cfg.output_dir / "2026-09-06" / "checklist.json").read_text(encoding="utf-8"))
    assert data["summary"]["fail"] == 1


# ── 연속 실패 ────────────────────────────────────────────────


def test_연속_실패_일수(tmp_path):
    out = tmp_path / "output"
    for d in ("2026-09-03",):
        (out / d).mkdir(parents=True); (out / d / "data.json").write_text("{}", encoding="utf-8")
    (out / "2026-09-05").mkdir()                      # 폴더만 있고 요약 없음
    assert failure_streak(out, date(2026, 9, 6)) == 3   # 6, 5, 4 실패 → 3 에서 멈춤
    assert failure_streak(out, date(2026, 9, 3)) == 0
    assert failure_streak(tmp_path / "없음", date(2026, 9, 6)) == 0        # 아무것도 없으면 0
    # 시작일(9-3) 이전은 세지 않는다: 9-2 만 보면 폴더가 없어도 0
    assert failure_streak(out, date(2026, 9, 2)) == 0


def test_실패_알림은_연속이면_원인_점검을_권한다():
    once = notify.build_failure_message(date="2026-09-06", streak=1)
    twice = notify.build_failure_message(date="2026-09-06", streak=2, run_url="https://x")
    assert "안전망" in once and "연속" not in once
    # 07:25 가 이미 지났으면 "한 번 더 시도합니다" 는 거짓말이다 (2026-09-09 아침)
    late = notify.build_failure_message(date="2026-09-06", streak=1, retry_ahead=False)
    assert "안전망" not in late and "자동 재시도가 없습니다" in late
    assert "2일 연속" in twice and "피드 점검" in twice and twice.endswith("https://x")


def test_대시보드에_연속_실패_배너(cfg, tmp_path):
    from rebrief.site import build_site
    for d in ("2026-09-05", "2026-09-06"):
        day = cfg.output_dir / d; day.mkdir(parents=True)
        (day / "brief.md").write_text("# b", encoding="utf-8")
    dest = build_site(cfg, tmp_path / "site")
    assert "2일 연속 요약 실패" in (dest / "dashboard.html").read_text(encoding="utf-8")


# ── 주간 추이 그림 ───────────────────────────────────────────


def test_주간_결산에_사흘_이상_지표의_추이가_붙는다(cfg, monkeypatch):
    from rebrief.weekly import run_weekly
    from tests.test_pipeline import FakeGenerator, _seed_days
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.weekly.ContentGenerator", FakeGenerator)
    _seed_days(cfg, ["2026-09-02", "2026-09-04", "2026-09-06"])
    store = SeriesStore(cfg.state_dir / "datapoints.json")
    for d, v in (("2026-09-02", "-0.05"), ("2026-09-04", "-0.04"), ("2026-09-06", "-0.03")):
        store.record(d, [{"label": "서울 아파트 주간 변동률", "value": v, "unit": "%"}])
    store.save()
    result = run_weekly(cfg, end_date="2026-09-06", use_llm=True)
    assert (result.out_dir / "img-time-series-1.svg").exists()
    assert any("추이 그림 1장" in w for w in result.warnings)


# ── PWA ──────────────────────────────────────────────────────


def test_사이트에_manifest_와_아이콘이_있다(cfg, tmp_path):
    from rebrief.site import build_site
    day = cfg.output_dir / "2026-09-06"; day.mkdir(parents=True)
    (day / "brief.md").write_text("# b", encoding="utf-8")
    dest = build_site(cfg, tmp_path / "site")
    m = json.loads((dest / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["display"] == "standalone" and (dest / "icon.svg").exists()
    assert not (dest / "icon-512.png").exists()                 # png=False 인 테스트 설정
    assert 'rel="manifest" href="manifest.webmanifest"' in (dest / "index.html").read_text(encoding="utf-8")
    assert 'href="../manifest.webmanifest"' in (dest / "2026-09-06" / "brief.html").read_text(encoding="utf-8")


def test_unverified_numbers_keep_lead_link_and_snippet_spacing():
    from rebrief.models import Article, Cluster, DailyBrief, DataPoint, IssueBrief
    from rebrief.verify import check_numbers

    art = Article(id="a1", title="서울 22개구 종부세 대상", url="https://x.test/1", feed_id="f", feed_name="F",
                  summary="", body="집값이 매년 11% 오르면 2030년에는 22개구가 대상이 된다. " + "본문 " * 200)
    cluster = Cluster(key="c1", articles=[art], score=1.0)
    issue = IssueBrief(title="종부세", one_liner="x", category="세금·절세", what_happened=["a"],
                       numbers=[DataPoint(label="상승률", value="11", unit="%"),
                                DataPoint(label="제외 자치구", value="3개구(강북·금천·도봉)", unit="개구"),
                                DataPoint(label="인원", value="71명", unit="명")],
                       why_it_matters="y", who_is_affected=[], caution="없음", source_urls=[art.url])
    brief = DailyBrief(date="2026-09-07", headline="h", lead="l", issues=[issue], market_temperature="t", tomorrow_watch=[])
    checks = {c.label: c for c in check_numbers(brief, [cluster])}
    assert checks["상승률"].status == "확인" and "매년 11% 오르면" in checks["상승률"].snippet   # 띄어쓰기 보존
    assert checks["제외 자치구"].status == "미확인" and checks["제외 자치구"].url == art.url    # 못 찾아도 대표 기사 링크
    assert checks["인원"].display == "71명"                                                     # '명명' 방지
