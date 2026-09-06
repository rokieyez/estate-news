"""부가 기능 검증 — 비용 장부, 수치 시계열, 링크 점검, 알림, 사이트 그림 복사."""

from __future__ import annotations

from datetime import date

import pytest

from rebrief import images, linkcheck, notify
from rebrief.llm import Usage
from rebrief.store import CostLog, SeriesStore


# ── 비용 장부 ────────────────────────────────────────────────


def _usage(calls=3, inp=10_000, out=20_000, model="claude-opus-5") -> Usage:
    u = Usage(model=model)
    u.calls, u.input_tokens, u.output_tokens = calls, inp, out
    return u


def test_비용은_날짜별로_쌓이고_같은_날_재실행은_더해진다(tmp_path):
    log = CostLog(tmp_path / "costs.json")
    log.record("2026-09-06", _usage())
    log.record("2026-09-06", _usage())          # 같은 날 render 를 한 번 더 돌린 경우
    log.record("2026-09-07", _usage(calls=1, inp=1000, out=1000))
    log.save()

    again = CostLog(tmp_path / "costs.json")
    by_date = again.by_date()
    assert list(by_date) == ["2026-09-06", "2026-09-07"]
    assert by_date["2026-09-06"] == pytest.approx(2 * _usage().estimated_usd, rel=1e-3)


def test_호출이_없으면_기록하지_않는다(tmp_path):
    log = CostLog(tmp_path / "costs.json")
    assert log.record("2026-09-06", _usage(calls=0)) == {}
    assert log.entries == []


def test_최근_N일_합계(tmp_path):
    log = CostLog(tmp_path / "costs.json")
    for d in ("2026-09-01", "2026-09-05", "2026-09-06"):
        log.record(d, _usage(calls=1, inp=1_000_000, out=0))   # 각 $5
    total, days = log.recent(3, today=date(2026, 9, 6))
    assert days == 2 and total == pytest.approx(10.0)


def test_깨진_장부는_비우고_다시_시작한다(tmp_path):
    path = tmp_path / "costs.json"
    path.write_text("{ 깨짐", encoding="utf-8")
    assert CostLog(path).entries == []


# ── 수치 시계열 ──────────────────────────────────────────────


def _dp(label, value, unit="%", **kw):
    d = {"label": label, "value": str(value), "unit": unit, "period": "", "source": "", "issue": ""}
    d.update(kw)
    return d


def test_같은_날짜_재실행은_행을_갈아끼운다(tmp_path):
    store = SeriesStore(tmp_path / "datapoints.json")
    store.record("2026-09-06", [_dp("서울 아파트 주간 변동률", -0.03)])
    store.record("2026-09-06", [_dp("서울 아파트 주간 변동률", -0.04)])
    assert len(store.rows) == 1 and store.rows[0]["value"] == "-0.04"


def test_추이는_사흘_이상_쌓여야_그린다():
    history = [
        {"date": "2026-09-04", **_dp("서울 아파트 주간 매매가격 변동률", -0.05)},
        {"date": "2026-09-05", **_dp("서울 아파트값 주간 변동률", -0.04)},     # 라벨이 조금 다름
    ]
    today = _dp("서울 아파트 주간 매매가격 변동률", -0.03)
    assert images.time_series(today, "2026-09-06", {"history": history}) is None

    history.append({"date": "2026-09-06", **today})
    img = images.time_series(today, "2026-09-06", {"history": history})
    assert img is not None and img.slug == "time-series"
    assert "-0.05%" in img.svg and "-0.03%" in img.svg      # 처음·끝 직접 라벨
    assert img.svg.count("<circle") == 3


def test_단위가_다르면_다른_지표다():
    history = [{"date": f"2026-09-0{i}", **_dp("거래량", 100 + i, unit="건")} for i in range(1, 5)]
    today = _dp("거래량", 40, unit="%")
    assert images.series_for(today, history) == []


def test_오늘_값이_없는_지표는_오늘_그림이_아니다():
    history = [{"date": f"2026-09-0{i}", **_dp("금리", 3 + i * 0.1)} for i in range(1, 5)]
    assert images.time_series(_dp("금리", 3.5), "2026-09-09", {"history": history}) is None


def test_추이가_있으면_다른_형태보다_먼저_고른다():
    history = [{"date": f"2026-09-0{i}", **_dp("공급 감소율", 70 + i)} for i in range(1, 4)]
    built = images.build([_dp("공급 감소율", 73, period="3년")], "2026-09-03", history=history)
    assert built and built[0].slug == "time-series"


# ── 링크 점검 ────────────────────────────────────────────────


class _Resp:
    def __init__(self, code):
        self.status_code = code

    def close(self):
        pass


def test_링크_점검은_상태코드로_판정한다(monkeypatch, tmp_path):
    from rebrief.config import load_config
    cfg = load_config()
    codes = {"https://a.test/1": 200, "https://a.test/2": 404, "https://a.test/3": 301}
    monkeypatch.setattr(linkcheck, "_head", lambda url, **kw: _Resp(codes[url]))
    monkeypatch.setattr(linkcheck, "_get", lambda url, **kw: _Resp(codes[url]))

    status = linkcheck.check_links(cfg, list(codes) + ["https://a.test/1"])   # 중복 포함
    assert len(status) == 3
    assert status["https://a.test/1"].ok and status["https://a.test/3"].ok
    assert not status["https://a.test/2"].ok and status["https://a.test/2"].note == "HTTP 404"


def test_HEAD_를_거부하면_GET_으로_다시_본다(monkeypatch):
    from rebrief.config import load_config
    cfg = load_config()
    calls = []
    monkeypatch.setattr(linkcheck, "_head", lambda url, **kw: calls.append("head") or _Resp(405))
    monkeypatch.setattr(linkcheck, "_get", lambda url, **kw: calls.append("get") or _Resp(200))
    status = linkcheck.check_links(cfg, ["https://b.test"])
    assert status["https://b.test"].ok and calls == ["head", "get"]


def test_연결_실패는_예외_이름을_남긴다(monkeypatch):
    from rebrief.config import load_config
    cfg = load_config()

    def boom(url, **kw):
        raise linkcheck.requests.ConnectionError("끊김")
    monkeypatch.setattr(linkcheck, "_head", boom)
    st = linkcheck.check_links(cfg, ["https://c.test"])["https://c.test"]
    assert not st.ok and st.note == "ConnectionError"


def test_죽은_링크는_sources_에_표시된다(cfg, monkeypatch):
    from rebrief import pipeline
    cfg.settings["collect"]["check_links"] = True
    monkeypatch.setattr(linkcheck, "_head", lambda url, **kw: _Resp(404 if url.endswith("/2") else 200))
    monkeypatch.setattr(linkcheck, "_get", lambda url, **kw: _Resp(404 if url.endswith("/2") else 200))

    result = pipeline.run(cfg, run_date="2026-09-06", use_llm=False)
    text = (cfg.output_dir / "2026-09-06" / "sources.md").read_text(encoding="utf-8")
    assert "링크 점검" in text
    if any("열리지 않습니다" in w for w in result.warnings):
        assert "⚠️ 열리지 않음 (HTTP 404)" in text


# ── 알림 ─────────────────────────────────────────────────────


def test_토큰이_없으면_보내지_않는다(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not notify.telegram_configured()
    assert notify.send_telegram("x") is False


def test_알림_문구(monkeypatch):
    text = notify.build_run_message(
        date="2026-09-06", headline="종부세 확산 경고", issues=5, articles=193,
        site_url="https://www.rokiz.net/estate-news/", warnings=["출처 링크 2개가 열리지 않습니다"],
        llm_used=True, images=3,
    )
    assert text.splitlines()[0] == "📅 2026-09-06 부동산 브리핑"
    assert "그림 3장" in text and "⚠️ 출처 링크" in text
    assert text.endswith("https://www.rokiz.net/estate-news/latest/")


def test_전송은_HTTP_200_일_때만_성공(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    sent = {}

    class R:
        status_code = 200
        text = ""
    monkeypatch.setattr(notify, "_post", lambda url, **kw: sent.update(kw, url=url) or R())
    assert notify.send_telegram("안녕") is True
    assert sent["json"] == {"chat_id": "c", "text": "안녕", "disable_web_page_preview": False}
    assert sent["url"] == "https://api.telegram.org/bott/sendMessage"


# ── 사이트 그림 복사 ─────────────────────────────────────────


def test_사이트에_그림과_모아보기_페이지가_들어간다(cfg, tmp_path):
    from rebrief.site import build_site
    day = cfg.output_dir / "2026-09-06"
    day.mkdir(parents=True)
    (day / "brief.md").write_text("# 브리핑", encoding="utf-8")
    (day / "img-district-map.png").write_bytes(b"\x89PNG")
    (day / "img-district-map.svg").write_text("<svg/>", encoding="utf-8")
    (day / "thumb-shorts.png").write_bytes(b"\x89PNG")
    (day / "shorts-draft.mp4").write_bytes(b"0000")

    dest = build_site(cfg, tmp_path / "site")
    assert (dest / "2026-09-06" / "img-district-map.png").exists()
    assert (dest / "latest" / "thumb-shorts.png").exists()
    assert not (dest / "2026-09-06" / "shorts-draft.mp4").exists()     # 영상은 사이트에 안 올린다
    gallery = (dest / "2026-09-06" / "images.html").read_text(encoding="utf-8")
    assert "서울 자치구 도식" in gallery and "쇼츠 썸네일" in gallery
    index = (dest / "index.html").read_text(encoding="utf-8")
    assert "그림·썸네일" in index


# ── INDEX 비용 절 ────────────────────────────────────────────


def test_INDEX_에_비용_절이_붙는다(cfg):
    from rebrief.render import update_index
    log = CostLog(cfg.state_dir / "costs.json")
    log.record("2026-09-06", _usage(calls=1, inp=1_000_000, out=0))    # $5
    log.save()
    text = update_index(cfg).read_text(encoding="utf-8")
    assert "## 비용 (실측)" in text and "$5.000" in text and "7,000원" in text


# ── 본문 자리에 그림 넣기 ────────────────────────────────────


def test_네이버_HTML_은_자리에_파일명과_미리보기를_붙인다():
    from rebrief.render import to_naver_html
    body = "첫 문단\n\n[이미지: 지도]\n\n둘째\n\n[이미지: 사진]\n"
    html = to_naver_html(body, {1: "img-1-district-map.png"})
    assert html.count("imgslot") == 2
    assert "img-1-district-map.png</b> 을 이 자리에" in html
    assert '<img class="preview nocopy" src="img-1-district-map.png" alt="지도">' in html
    assert "📷 이미지 — 사진</div>" in html          # 2번 자리는 그대로 점선 상자


def test_마크다운_판은_실제_이미지_문법으로_바꾼다():
    from rebrief.render import place_images_markdown
    body = "[이미지: 지도]\n\n[이미지: 사진]"
    out = place_images_markdown(body, {1: "img-1-district-map.png"})
    assert out == "![지도](img-1-district-map.png)\n\n[이미지: 사진]"
    assert place_images_markdown(body, {}) == body
