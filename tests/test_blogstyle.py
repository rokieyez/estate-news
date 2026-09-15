"""블로그 첨부 그림의 사진 배경 판 (rebrief/blogstyle.py, 2026-09-15). 두 저장소 공통 파일입니다."""

from __future__ import annotations

import re

import pytest

from rebrief import blogstyle, images

DATE = "2026-09-15"
CREDIT = "사진 홍길동 / Pexels · 본문과 무관"


def dp(**kw) -> dict:
    base = {"label": "수치", "value": "1", "unit": "", "period": "", "context": "", "source": "연합뉴스"}
    base.update(kw)
    return base


def _photo(tmp_path):
    path = tmp_path / "photo-1.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 not a real jpeg")      # 시험은 PNG 로 바꾸지 않아 풀어 볼 일이 없다
    return [(path, CREDIT)]


@pytest.mark.parametrize("style", sorted(blogstyle.STYLES))
def test_backdrop_lays_the_photo_under_the_chart_and_restores_the_palette(tmp_path, style):
    before = (images.INK, images.BLUE, images.FONT)
    with blogstyle.backdrop(style, _photo(tmp_path)):
        img = images.stat_card(dp(label="공급 물량", value="3200", unit="가구"), DATE)
    assert "data:image/jpeg;base64," in img.svg
    assert "본문과 무관" in img.svg                           # 사진 출처와 '무관' 표기는 빠지면 안 된다
    assert f'fill="{images.SURFACE}"' not in img.svg         # 흰 바탕이 남으면 사진을 덮는다
    assert "3,200" in img.svg
    assert (images.INK, images.BLUE, images.FONT) == before and images._BACKDROP is None


def test_backdrop_without_photos_keeps_the_dark_face():
    with blogstyle.backdrop("cinema", []):
        img = images.stat_card(dp(value="12", unit="건"), DATE)
    assert "<image" not in img.svg and "bd-base" in img.svg


def test_unknown_style_draws_the_old_white_card():
    with blogstyle.backdrop("", []) as styled:
        img = images.stat_card(dp(value="12", unit="건"), DATE)
    assert styled is False
    assert f'fill="{images.SURFACE}"' in img.svg


def _luminance(color: str) -> float:
    c = color.lstrip("#")
    rgb = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


@pytest.mark.parametrize("style", sorted(blogstyle.STYLES))
def test_small_text_stays_readable_on_a_white_photo(style):
    """그날 사진이 흰 하늘이어도 덮개(+유리판)만으로 작은 글씨 대비 4.5 를 넘겨야 한다."""
    spec = blogstyle.STYLES[style]
    grey = 255 * (1 - spec["shade"])
    if spec["panel"]:
        grey += (255 - grey) * blogstyle.PANEL_FILL
    ground = _luminance("#%02x%02x%02x" % ((round(grey),) * 3))
    for name in ("INK", "INK_2", "MUTED", "BLUE"):
        ratio = (_luminance(spec["palette"][name]) + 0.05) / (ground + 0.05)
        assert ratio >= 4.5, (style, name, round(ratio, 2))


def test_two_numbers_of_one_issue_become_a_bar_comparison():
    pool = [dp(label="과천 아파트 보유 기간", value="17", unit="년", issue="청문회"),
            dp(label="과천 아파트 실거주 기간", value="4", unit="개월", issue="청문회")]
    with blogstyle.backdrop("glass", []):
        built = images.build(pool, DATE)
    assert len(built) == 1                                   # 둘째 수치는 첫 그림에 이미 실렸다
    svg = built[0].svg
    text = "".join(re.findall(r">([^<>]*)<", svg))
    assert "보유 기간" in text and "실거주 기간" in text
    assert text.count("과천 아파트") == 1                    # 겹치는 앞말은 제목에 한 번만


def test_numbers_that_measure_different_things_are_not_paired():
    """2026-09-14 부동산 자료 — 오른 폭과 단가는 단위가 같아도 견주지 않는다."""
    pool = [dp(label="인천 아파트 분양가 상승액", value="11000", unit="만원", issue="분양가"),
            dp(label="인천 아파트 3.3㎡당 분양가", value="2000", unit="만원", issue="분양가")]
    assert blogstyle.pair_of(pool[0], pool) is None and blogstyle.pair_of(pool[1], pool) is None
    unrelated = [dp(label="발의 법안", value="147", unit="건", issue="국회"),
                 dp(label="체포 동의안 표결 찬성", value="160", unit="건", issue="국회")]
    assert blogstyle.pair_of(unrelated[0], unrelated) is None     # 나누는 낱말이 없다
    witnesses = [dp(label="요청한 증인 수", value="44", unit="명", issue="청문회"),
                 dp(label="채택된 증인 수", value="4", unit="명", issue="청문회")]
    assert blogstyle.pair_of(witnesses[0], witnesses) is witnesses[1]


def test_change_rates_are_never_drawn_as_a_share():
    assert blogstyle.share_value(dp(label="전세가율", value="54.8", unit="%")) == 54.8
    assert blogstyle.share_value(dp(label="매매가 상승률", value="3", unit="%")) is None
    assert blogstyle.share_value(dp(label="응답 비율 차이", value="3", unit="%p")) is None


def test_poll_numbers_become_bars_with_the_disclosure_line():
    rows = [dp(label="대통령 국정수행 긍정평가", value="37.4", unit="%", issue="지지율"),
            dp(label="대통령 국정수행 부정평가", value="55.1", unit="%", issue="지지율")]
    built = images.build(rows, DATE)
    assert [b.slug for b in built] == ["poll-chart"]
    svg = built[0].svg
    assert "37.4%" in svg and "55.1%" in svg
    assert "중앙선거여론조사심의위원회" in svg
    assert "보도에 없음" in svg                                # 개요가 없으면 없다고 적고 지어내지 않는다


def test_poll_chart_takes_the_overview_from_the_registry_when_the_agency_is_named():
    label = "정당 지지도 (여론조사꽃 조사)"
    polls = [{"agency": "(주)여론조사꽃",
              "summary": "(주)여론조사꽃 · 2026-09-11~12 · 1,002명 · 무선전화면접 · 응답률 10.1% · 95% 신뢰수준에 ±3.1%p"}]
    by_slot, _ = images.build_for_slots([dp(label=label, value="41", unit="%", issue="정당")], DATE, [label],
                                        polls=polls)
    svg = by_slot[1].svg
    assert "1,002명" in svg and "오차범위 ±3.1%p" in svg


def test_poll_point_changes_are_not_poll_bars():
    assert images.poll_chart(dp(label="지지율 하락폭", value="3.1", unit="%p"), DATE, {}) is None
    assert images.poll_chart(dp(label="지지율 하락", value="3.1", unit="%"), DATE, {}) is None


@pytest.mark.parametrize("style", sorted(blogstyle.STYLES))
def test_cover_keeps_title_badge_channel_and_credit(tmp_path, style):
    with blogstyle.backdrop(style, _photo(tmp_path)):
        img = blogstyle.cover("장관 후보자 인사청문회 일정, 9월 15일 국회 정리", badge="17년",
                              channel="방구석 배경지식", date=DATE)
    text = "".join(re.findall(r">([^<>]*)<", img.svg))
    for word in ("인사청문회", "17년", "방구석 배경지식", "본문과 무관", "2026.09.15"):
        assert word in text
    assert 'width="1200" height="630"' in img.svg
