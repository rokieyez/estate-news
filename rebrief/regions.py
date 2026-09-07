"""지역명 뽑기 — 부동산 검색은 지역이 절반이다.

오늘 브리핑에 나온 자치구·시 이름을 찾아 대표 검색어 후보·소제목·태그에 쓴다.
한국어는 부분 문자열이 겹치므로('성동구' 안의 '동구', '중구청' 안의 '중구')
앞뒤에 한글이 붙지 않은 것만 지역으로 본다.
"""

from __future__ import annotations

import re

# 서울 25개 자치구
SEOUL = [
    "강남구", "강동구", "강북구", "강서구", "관악구", "광진구", "구로구", "금천구", "노원구",
    "도봉구", "동대문구", "동작구", "마포구", "서대문구", "서초구", "성동구", "성북구", "송파구",
    "양천구", "영등포구", "용산구", "은평구", "종로구", "중구", "중랑구",
]
# 광역시·도청 소재지와 부동산 기사에 자주 나오는 시
CITIES = [
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
    "수원", "성남", "고양", "용인", "부천", "안산", "안양", "남양주", "화성", "평택", "의정부",
    "시흥", "파주", "김포", "광명", "군포", "하남", "오산", "이천", "안성", "구리", "과천",
    "청주", "천안", "전주", "포항", "창원", "김해", "제주",
]
# 이름 자체가 상권·학군으로 통하는 곳 (기사에 자주 등장)
AREAS = [
    "강남", "여의도", "목동", "상계", "판교", "분당", "일산", "동탄", "위례", "마곡",
    "잠실", "반포", "압구정", "청담", "성수", "왕십리", "도곡", "개포", "둔촌", "흑석",
]

# 자치구를 '강북·금천·도봉' 처럼 구 없이 쓰는 기사가 많다. 줄임말을 정식 이름으로 되돌린다.
# '중구' → '중', '동작구' → '동작' 은 다른 뜻으로 흔히 쓰여 뺀다.
_STEM_SKIP = {"중", "동작"}
STEMS = {name[:-1]: name for name in SEOUL if len(name) > 2 and name[:-1] not in _STEM_SKIP}

# 긴 이름을 먼저 찾아야 '강남구' 가 '강남' 으로 잘리지 않는다
# 길이 우선, 같은 길이면 가나다 순 — 집합 순회 순서에 따라 결과가 흔들리지 않게 못박는다
_NAMES = sorted(set(SEOUL + CITIES + AREAS) | set(STEMS), key=lambda n: (-len(n), n))
_HANGUL = r"가-힣"
# 뒤에 붙을 수 있는 것은 조사뿐이다. '중구청' 의 '청' 은 조사가 아니므로 지역으로 보지 않는다.
_PARTICLES = ("은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "로", "으로",
              "에서", "부터", "까지", "만", "보다", "처럼", "및", "산", "권")
_TRAILING = "|".join(sorted(_PARTICLES, key=len, reverse=True))
_PATTERN = re.compile(
    r"(?<![" + _HANGUL + r"])(" + "|".join(re.escape(n) for n in _NAMES) + r")"
    r"(?:(?=[^" + _HANGUL + r"])|(?=(?:" + _TRAILING + r")(?![" + _HANGUL + r"]))|$)"
)


def canonical(name: str) -> str:
    """'강북' 을 '강북구' 로. 정식 이름이면 그대로."""
    return STEMS.get(name, name)


def find_regions(text: str, limit: int = 6) -> list[str]:
    """글에 나온 지역명을 많이 나온 순으로. 같은 자리를 가리키는 이름은 정식 이름으로 합친다."""
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for match in _PATTERN.finditer(text or ""):
        name = canonical(match.group(1))
        counts[name] = counts.get(name, 0) + 1
        first.setdefault(name, match.start())
    # '강남구' 가 있으면 '강남' 은 뺀다 (같은 곳을 두 번 세지 않게)
    for name in list(counts):
        if any(other != name and name in other for other in counts):
            counts.pop(name, None)
    # 많이 나온 순, 같으면 글에 먼저 나온 순
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], first[kv[0]]))
    return [name for name, _ in ordered[:limit]]


def from_brief(brief, limit: int = 6) -> list[str]:
    """브리핑 전체(제목·한 줄·사실·수치 라벨)에서 지역명을 모은다."""
    if brief is None:
        return []
    chunks = [brief.headline, getattr(brief, "lead", "") or ""]
    for issue in brief.issues:
        chunks += [issue.title, issue.one_liner]
        chunks += list(issue.what_happened or [])
        chunks += [f"{n.label} {n.value}{n.unit} {n.context}" for n in issue.numbers]
    return find_regions("\n".join(c for c in chunks if c), limit)
