"""인포그래픽 생성 검증 — 네트워크·브라우저 없이 SVG 문자열만 본다."""

from __future__ import annotations

import re

import pytest

from rebrief import images

DATE = "2026-09-06"


def dp(**kw) -> dict:
    base = {"label": "", "value": "", "unit": "", "period": "", "context": "", "source": ""}
    base.update(kw)
    return base


# ── 서울 자치구 도식 ─────────────────────────────────────────


def test_자치구_도식은_보도값과_칸수가_맞을_때만_그린다():
    img = images.seoul_district_map(
        dp(label="2030년 종부세 과세 대상 서울 자치구 수", value="22", unit="개구",
           context="강북·금천·도봉 3개구만 제외", source="뉴스1"),
        DATE,
    )
    assert img is not None
    # 25개 자치구 이름이 모두 들어가야 한다
    for gu in images.SEOUL_GU:
        assert f">{gu}<" in img.svg
    # 제외 3곳은 점선으로, 나머지는 파랑으로
    assert img.svg.count("stroke-dasharray") == 3
    assert img.svg.count(f'fill="{images.BLUE}"') == 22 + 1   # 칸 22개 + 범례 1개


def test_보도값과_칸수가_어긋나면_그리지_않는다():
    # 21 은 25 - 3 과 맞지 않는다. 틀린 그림보다 없는 편이 낫다.
    assert images.seoul_district_map(
        dp(label="서울 자치구 수", value="21", context="강북·금천·도봉 3개구만 제외"), DATE
    ) is None


def test_제외_목록이_없으면_그리지_않는다():
    assert images.seoul_district_map(
        dp(label="서울 자치구 수", value="22", context="전 지역이 대상"), DATE
    ) is None


def test_자치구_도식_레이아웃은_25개구다():
    assert len(images.SEOUL_GU) == 25
    flat = [gu for row in images.SEOUL_LAYOUT for gu in row.values()]
    assert len(flat) == 25, "중복된 자치구가 있습니다"


# ── 지수 비교 ────────────────────────────────────────────────


def test_감소율은_지수막대로_그린다():
    img = images.index_comparison(
        dp(label="임대주택 공급 감소율", value="81", unit="%", period="최근 3년",
           context="기사 제목에 제시된 수치", source="매일경제"),
        DATE,
    )
    assert img is not None
    assert ">100<" in img.svg and ">19<" in img.svg     # 100 → 19
    assert "▼" in img.svg and "감소" in img.svg          # 색만으로 뜻을 전하지 않는다
    # 없는 숫자를 지어내지 않았다고 밝혀야 한다
    assert "제시되지 않았습니다" in img.svg


def test_증가율은_위쪽_화살표로_그린다():
    img = images.index_comparison(
        dp(label="거래량 증가율", value="40", unit="%", period="1년"), DATE
    )
    assert img is not None
    assert "▲" in img.svg and ">140<" in img.svg


def test_퍼센트가_아니면_지수막대를_쓰지_않는다():
    assert images.index_comparison(
        dp(label="공급 감소율", value="3", unit="만호", period="3년"), DATE
    ) is None


# ── 수치 카드 ────────────────────────────────────────────────


def test_수치_카드는_어떤_수치든_받는다():
    img = images.stat_card(
        dp(label="서울 아파트 주간 매매가격 변동률", value="-0.03", unit="%",
           period="9월 첫째 주", context="전주 -0.05%에서 낙폭 축소", source="한국부동산원"),
        DATE,
    )
    assert img is not None
    assert "-0.03" in img.svg and "한국부동산원" in img.svg


def test_값이_비면_카드도_만들지_않는다():
    assert images.stat_card(dp(label="빈 수치"), DATE) is None


# ── 조립 ─────────────────────────────────────────────────────


def test_수치별로_알맞은_형태를_고른다():
    built = images.build([
        dp(label="2030년 종부세 과세 대상 서울 자치구 수", value="22", unit="개구",
           context="강북·금천·도봉 3개구만 제외"),
        dp(label="임대주택 공급 감소율", value="81", unit="%", period="최근 3년"),
        dp(label="롯데건설 누적 수주액", value="4", unit="조원"),
    ], DATE)
    assert [i.slug for i in built] == ["district-map", "index-comparison", "stat-card"]


def test_최대_장수를_넘기지_않는다():
    many = [dp(label=f"수치 {i}", value=str(i)) for i in range(10)]
    assert len(images.build(many, DATE, limit=2)) == 2


def test_같은_형태가_겹치면_파일명이_갈린다():
    built = images.build([dp(label="가", value="1"), dp(label="나", value="2")], DATE)
    assert [i.slug for i in built] == ["stat-card", "stat-card-2"]


def test_그릴_수_없는_날은_빈_목록을_낸다():
    assert images.build([], DATE) == []


def test_생성기가_터져도_파이프라인을_멈추지_않는다(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("의도된 실패")
    monkeypatch.setattr(images, "SPECIFIC", (boom,))
    built = images.build([dp(label="수치", value="1")], DATE)
    assert len(built) == 1        # 터진 생성기는 건너뛰고 다음 것으로 넘어간다


# ── SVG 형식 ─────────────────────────────────────────────────


@pytest.mark.parametrize("payload", [
    dp(label="2030년 종부세 과세 대상 서울 자치구 수", value="22",
       context="강북·금천·도봉 3개구만 제외"),
    dp(label="공급 감소율", value="81", unit="%", period="최근 3년"),
    dp(label="누적 수주액", value="4", unit="조원"),
])
def test_만들어진_svg는_파싱된다(payload):
    import xml.etree.ElementTree as ET
    built = images.build([payload], DATE)
    assert built
    root = ET.fromstring(built[0].svg)       # 깨진 XML 이면 여기서 터진다
    assert root.tag.endswith("svg")


def test_꺾쇠는_이스케이프된다():
    img = images.stat_card(dp(label="<script>", value="1"), DATE)
    assert "<script>" not in img.svg and "&lt;script&gt;" in img.svg


def test_막대_경로는_길이가_0이어도_유효하다():
    assert images.bar_path(0, 0, 0, 56).startswith("M0,0")


# ── 본문 자리 매칭 ───────────────────────────────────────────


def test_라벨이_정확히_같으면_그_수치를_고른다():
    dps = [dp(label="가", value="1"), dp(label="나", value="2")]
    assert images.match_datapoint("나", dps) == 1


def test_라벨이_조금_달라도_비슷하면_고른다():
    dps = [dp(label="서울 아파트 주간 매매가격 변동률", value="-0.03")]
    assert images.match_datapoint("서울 아파트 주간 매매가격 변동율", dps) == 0
    assert images.match_datapoint("전혀 다른 지표", dps) is None
    assert images.match_datapoint("", dps) is None


def test_자리마다_번호가_붙고_남는_자리는_여분으로_채운다():
    dps = [
        dp(label="2030년 종부세 과세 대상 서울 자치구 수", value="22", unit="개구",
           context="강북·금천·도봉 3개구만 제외"),
        dp(label="임대주택 공급 감소율", value="81", unit="%", period="최근 3년"),
        dp(label="누적 수주액", value="4", unit="조원"),
    ]
    # 자리 1 = 사진(빈 라벨), 자리 2 = 자치구, 자리 3 = 없는 라벨
    by_slot, extras = images.build_for_slots(
        dps, DATE, ["", "2030년 종부세 과세 대상 서울 자치구 수", "없는 지표"], limit=3)
    assert list(by_slot) == [2] and by_slot[2].slug == "2-district-map"
    assert [e.slug for e in extras] == ["index-comparison", "stat-card"]   # 쓴 수치는 다시 안 쓴다


def test_같은_수치를_두_자리가_가리키면_한_번만_그린다():
    dps = [dp(label="가", value="1")]
    by_slot, _ = images.build_for_slots(dps, DATE, ["가", "가"], limit=3)
    assert list(by_slot) == [1]


# ── 썸네일 ───────────────────────────────────────────────────


def test_썸네일은_긴_문구를_세_줄_안에_넣는다():
    img = images.thumbnail("종부세 대상 22개구 확대 전망 집값 매년 11% 오르면 2030년 서울 전역",
                           sub="9월 6일 부동산 브리핑", channel="부동산 브리핑", date=DATE)
    import xml.etree.ElementTree as ET
    root = ET.fromstring(img.svg)
    assert root.get("width") == "1280" and root.get("height") == "720"
    big = [t for t in root.iter("{http://www.w3.org/2000/svg}text") if t.get("font-weight") == "800"]
    assert 1 <= len(big) <= 3


def test_쇼츠_썸네일은_세로다():
    img = images.thumbnail("서울 22개구 종부세?", size=(1080, 1920))
    assert 'width="1080" height="1920"' in img.svg
