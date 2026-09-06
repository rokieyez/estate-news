"""2차 디벨롭 — 수치 검산, 모델 강등, 제목 장부, 주간 반복 이슈, 대시보드, 사진 링크."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebrief import verify
from rebrief.models import Article, Cluster, DailyBrief, DataPoint, IssueBrief
from rebrief.store import TitleLog, title_type


# ── 수치 검산 ────────────────────────────────────────────────


def _cluster(url, title, body=""):
    return Cluster(key=url, articles=[Article(id=Article.make_id(url, title), title=title, url=url,
                                                feed_id="f", feed_name="테스트", body=body)])


def _issue(title, numbers, urls):
    return IssueBrief(title=title, one_liner="x", category="가격동향", what_happened=["a"],
                      numbers=numbers, why_it_matters="y", who_is_affected=["z"], caution="없음",
                      source_urls=urls)


def _brief(*issues):
    return DailyBrief(date="2026-09-06", headline="h", lead="l", issues=list(issues),
                      market_temperature="m", tomorrow_watch=[])


def test_값이_본문에_있으면_확인이다():
    body = "서울 아파트값이 지난주 -0.03% 내렸다. " * 20
    clusters = [_cluster("https://a/1", "서울 아파트값 하락", body)]
    brief = _brief(_issue("가격", [DataPoint(label="주간 변동률", value="-0.03", unit="%")], ["https://a/1"]))
    [c] = verify.check_numbers(brief, clusters)
    assert c.status == verify.VERIFIED


def test_값이_없으면_미확인이다():
    body = "서울 아파트값이 내렸다. " * 30
    clusters = [_cluster("https://a/1", "하락", body)]
    brief = _brief(_issue("가격", [DataPoint(label="변동률", value="-0.07", unit="%")], ["https://a/1"]))
    [c] = verify.check_numbers(brief, clusters)
    assert c.status == verify.NOT_FOUND


def test_본문이_없으면_대조_불가다():
    clusters = [_cluster("https://a/1", "서울 아파트값 하락")]     # 제목뿐
    brief = _brief(_issue("가격", [DataPoint(label="변동률", value="-0.07", unit="%")], ["https://a/1"]))
    [c] = verify.check_numbers(brief, clusters)
    assert c.status == verify.NO_TEXT


def test_콤마와_범위_표기를_너그럽게_본다():
    body = "누적 수주액 41,200억원. 임대수익률은 1∼2%대. 4조 1,200억이라고도 쓴다. " * 12
    clusters = [_cluster("https://a/1", "t", body)]
    brief = _brief(_issue("i", [DataPoint(label="수주", value="41200", unit="억"),
                                DataPoint(label="수익률", value="1~2", unit="%"),
                                DataPoint(label="환산", value="4.12", unit="조")], ["https://a/1"]))
    # 콤마·물결 표기는 너그럽게, 단위 환산(4.12조)은 추측하지 않는다 → 미확인
    assert [c.status for c in verify.check_numbers(brief, clusters)] == [verify.VERIFIED, verify.VERIFIED, verify.NOT_FOUND]


def test_다른_이슈_기사에서_찾으면_비고를_남긴다():
    clusters = [_cluster("https://a/1", "t", "x " * 200), _cluster("https://b/1", "u", "값은 22개구다. " * 30)]
    brief = _brief(_issue("i", [DataPoint(label="구", value="22", unit="개구")], ["https://a/1"]))
    [c] = verify.check_numbers(brief, clusters)
    assert c.status == verify.VERIFIED and "다른 이슈" in c.hint


def test_검산_결과가_brief_와_경고에_들어간다(cfg, monkeypatch):
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    result = pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    text = (cfg.output_dir / "2026-09-06" / "brief.md").read_text(encoding="utf-8")
    assert "## 숫자 검산" in text


# ── 모델 강등 ────────────────────────────────────────────────


class _Usage:
    input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 1000, 500, 0, 0


class _Resp:
    usage = _Usage()
    stop_reason = "end_turn"

    def __init__(self, parsed):
        self.parsed_output = parsed


def _generator(cfg, monkeypatch, behaviour):
    """behaviour(model) → 응답 또는 예외를 던지는 가짜 클라이언트."""
    import anthropic
    from rebrief.llm import ContentGenerator
    cfg.settings["llm"]["fallback_model"] = "claude-sonnet-5"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    gen = ContentGenerator.__new__(ContentGenerator)
    ContentGenerator.__init__(gen, cfg)

    class Msgs:
        def parse(self, **kw):
            return behaviour(kw["model"])

    gen.client = type("C", (), {"messages": Msgs()})()
    return gen, anthropic


def test_한도에_걸리면_대체_모델로_다시_시도한다(cfg, monkeypatch):
    from rebrief.models import BlogPost
    calls = []

    def behaviour(model):
        calls.append(model)
        if model == "claude-opus-5":
            raise anthropic.RateLimitError("limit", response=_FakeHTTP(429), body=None)
        return _Resp(BlogPost(title="t", slug="s", meta_description="m", tags=[], body_markdown="b"))

    gen, anthropic = _generator(cfg, monkeypatch, lambda m: behaviour(m))
    post = gen._parse(system="s", user="u", output_format=BlogPost, cache_system=False)
    assert post.title == "t" and calls == ["claude-opus-5", "claude-sonnet-5"]
    assert gen.usage.models_used == ["claude-sonnet-5"]
    assert any("claude-sonnet-5 로 생성" in n for n in gen.usage.notes)
    # 비용은 실제로 쓴 모델(sonnet) 단가로
    assert gen.usage.estimated_usd == pytest.approx(1000 / 1e6 * 2 + 500 / 1e6 * 10)


def test_인증_오류는_강등하지_않는다(cfg, monkeypatch):
    from rebrief.llm import LLMError
    from rebrief.models import BlogPost
    calls = []

    def behaviour(model):
        calls.append(model)
        raise anthropic.AuthenticationError("bad key", response=_FakeHTTP(401), body=None)

    gen, anthropic = _generator(cfg, monkeypatch, lambda m: behaviour(m))
    with pytest.raises(LLMError, match="유효하지"):
        gen._parse(system="s", user="u", output_format=BlogPost, cache_system=False)
    assert calls == ["claude-opus-5"]


def test_대체_모델도_실패하면_둘_다_적는다(cfg, monkeypatch):
    from rebrief.llm import LLMError
    from rebrief.models import BlogPost

    def behaviour(model):
        raise anthropic.APIStatusError("overloaded", response=_FakeHTTP(529), body=None)

    gen, anthropic = _generator(cfg, monkeypatch, lambda m: behaviour(m))
    with pytest.raises(LLMError, match="대체 모델 claude-sonnet-5 도 실패"):
        gen._parse(system="s", user="u", output_format=BlogPost, cache_system=False)


class _FakeHTTP:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.request = None

    def json(self):
        return {}

    @property
    def text(self):
        return ""


# ── 제목 장부 ────────────────────────────────────────────────


def test_제목_유형_분류():
    assert title_type("2030년 서울 22개구가 종부세 대상?") == "질문형"
    assert title_type("서울 vs 경기 집값") == "비교형"
    assert title_type("집값 11% 오르면") == "수치형"
    assert title_type("종부세 확산 경고") == "서술형"


def test_후보_기록과_선택(tmp_path):
    log = TitleLog(tmp_path / "titles.json")
    log.record_candidates("2026-09-06", "longform", ["a?", "b 3%", "c"])
    log.log_pick("2026-09-06", "longform", 2, views=1200)
    log.record_candidates("2026-09-06", "longform", ["a?", "b 3%", "c"])   # 재실행해도 선택은 유지
    log.save()
    again = TitleLog(tmp_path / "titles.json")
    [row] = again.picked()
    assert row["title"] == "b 3%" and row["type"] == "수치형" and row["views"] == 1200
    assert again.by_type() == {"수치형": {"count": 1, "avg_views": 1200}}


def test_후보_밖_번호는_제목이_있어야_한다(tmp_path):
    log = TitleLog(tmp_path / "titles.json")
    log.record_candidates("2026-09-06", "blog", ["a"])
    with pytest.raises(ValueError):
        log.log_pick("2026-09-06", "blog", 5)
    e = log.log_pick("2026-09-06", "blog", 5, title="직접 쓴 제목")
    assert e["title"] == "직접 쓴 제목"


def test_실행하면_제목_후보가_장부에_남는다(cfg, monkeypatch):
    from rebrief import pipeline
    from tests.test_pipeline import FakeGenerator
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("rebrief.pipeline.ContentGenerator", FakeGenerator)
    pipeline.run(cfg, run_date="2026-09-06", use_llm=True)
    log = TitleLog(cfg.state_dir / "titles.json")
    day = log.days["2026-09-06"]
    assert set(day) == {"blog", "longform", "shorts"} and day["longform"]["candidates"]


def test_titles_명령(cfg, monkeypatch, capsys):
    from rebrief import cli
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)
    TitleLog(cfg.state_dir / "titles.json").record_candidates("2026-09-06", "shorts", ["첫째?", "둘째"])
    log = TitleLog(cfg.state_dir / "titles.json"); log.record_candidates("2026-09-06", "shorts", ["첫째?", "둘째"]); log.save()
    assert cli.main(["titles", "log", "--date", "2026-09-06", "--kind", "shorts", "--pick", "1", "--views", "300"]) == 0
    assert "질문형" in capsys.readouterr().out
    assert cli.main(["titles", "show", "--date", "2026-09-06"]) == 0
    assert "고름: 1번" in capsys.readouterr().out
    assert "## 제목 기록" in (cfg.output_dir / "INDEX.md").read_text(encoding="utf-8")


# ── 주간 반복 이슈 ───────────────────────────────────────────


def test_여러_날_반복된_이슈를_묶는다():
    from rebrief.weekly import mark_streaks
    days = [
        {"date": "2026-09-01", "issues": [{"title": "서울 아파트값 3주 연속 하락"}, {"title": "전세사기 지원 확대"}]},
        {"date": "2026-09-02", "issues": [{"title": "서울 아파트값 4주 연속 하락"}]},
        {"date": "2026-09-03", "issues": [{"title": "롯데건설 도곡우성 수주"}]},
    ]
    streaks = mark_streaks(days)
    assert [s["dates"] for s in streaks] == [["2026-09-01", "2026-09-02"]]
    assert days[1]["issues"][0]["days_seen"] == ["2026-09-01", "2026-09-02"]
    assert "days_seen" not in days[2]["issues"][0]


def test_반복_이슈가_프롬프트에_들어간다(cfg):
    from rebrief.prompts import build_weekly_messages
    from rebrief.weekly import mark_streaks
    days = [{"date": "2026-09-01", "headline": "h", "issues": [{"title": "종부세 확산"}]},
            {"date": "2026-09-02", "headline": "h", "issues": [{"title": "종부세 확산 경고"}]}]
    mark_streaks(days)
    system, _ = build_weekly_messages(cfg, days, "2026-W36")
    assert "여러 날 반복된 이슈" in system and "2일 (2026-09-01, 2026-09-02)" in system


# ── 대시보드 ─────────────────────────────────────────────────


def test_대시보드가_비용과_실행을_모은다(cfg, tmp_path):
    from rebrief.site import build_site
    from rebrief.store import CostLog
    from rebrief.llm import Usage
    for d, llm in (("2026-09-05", False), ("2026-09-06", True)):
        day = cfg.output_dir / d
        (day / "raw").mkdir(parents=True)
        (day / "raw" / "articles.json").write_text(json.dumps({
            "meta": {"feeds": [{"id": "a", "ok": True}, {"id": "b", "ok": llm}]},
            "articles": [{"id": "1", "title": "t", "url": "https://x/1", "feed_id": "a", "feed_name": "a"}] * 3,
        }), encoding="utf-8")
        (day / "brief.md").write_text("# b", encoding="utf-8")
        if llm:
            (day / "data.json").write_text("{}", encoding="utf-8")
            (day / "img-1-stat-card.png").write_bytes(b"\x89PNG")
        else:
            (day / "prompt-pack.md").write_text("p", encoding="utf-8")
    u = Usage(model="claude-opus-5"); u.calls, u.input_tokens = 1, 1_000_000
    log = CostLog(cfg.state_dir / "costs.json"); log.record("2026-09-06", u); log.save()

    dest = build_site(cfg, tmp_path / "site")
    html = (dest / "dashboard.html").read_text(encoding="utf-8")
    assert "운영 현황" in html and "$5.000" in html
    assert 'class="bad">없음' in html and 'class="ok">생성' in html     # 5일은 요약 실패, 6일은 성공
    assert "키 없음/실패 → 프롬프트 팩" in html
    assert "dashboard.html" in (dest / "index.html").read_text(encoding="utf-8")


# ── 사진 검색 링크 ───────────────────────────────────────────


def test_사진_자리에는_검색_링크가_붙고_복사에서_빠진다():
    from rebrief.render import photo_search_links, to_naver_html
    links = photo_search_links("seoul apartment skyline")
    assert links[0][0] == "언스플래시" and "seoul-apartment-skyline" in links[0][1]
    assert photo_search_links("서울 아파트")[0][0] == "픽사베이"
    html = to_naver_html("[이미지: 서울 아파트 단지 전경]", {}, {1: links})
    assert 'class="photo-links nocopy"' in html and "unsplash.com" in html


def test_그림이_있는_자리에는_검색_링크가_없다():
    from rebrief.render import to_naver_html
    html = to_naver_html("[이미지: 지도]", {1: "img-1-district-map.png"}, {1: [("x", "https://x")]})
    assert "photo-links" not in html
