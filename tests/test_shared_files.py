"""두 저장소(estate-news · politics-news)에 **똑같이 있어야 하는** 파일이 한쪽만 고쳐지지 않았는지.

politics-news 는 estate-news 의 포크라 구독 호출·TTS 뽑기·HTML 청소 같은 뼈대가 같습니다.
한쪽만 고치면 다른 쪽에서 같은 버그가 다시 납니다 (2026-09-13 사용자 선택, 마무리 아이디어 7).

옆 저장소는 맥에서는 같은 폴더(Active/)의 옆자리를, 러너에서는 테스트 워크플로가 받아 둔
`REBRIEF_SIBLING` 을 봅니다. 옆 저장소를 찾을 수 없으면 건너뜁니다.
**일부러 다르게 둘 파일이면 아래 목록에서 빼세요** — 두 저장소 모두에서.
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAIR = {"estate-news": "politics-news", "politics-news": "estate-news"}

# 주제(부동산·정치)와 상관없는 뼈대만 담았습니다. 2026-09-13 에 두 저장소가 바이트까지 같았던 파일 중에서
# 골랐고, 부동산·정치 자료를 다루는 cluster·rank·regions·결산 틀은 뺐습니다(달라져도 되는 자리).
SHARED = [
    ".github/workflows/test.yml",
    ".github/workflows/publish-log.yml",
    ".github/workflows/title-log.yml",
    "rebrief/__main__.py",
    "rebrief/keynumbers.py",
    "rebrief/linkcheck.py",
    "rebrief/related.py",
    "rebrief/sanitize.py",
    "rebrief/subscription.py",
    "rebrief/tts.py",
    "rebrief/verify.py",
    "rebrief/templates/script_shorts.md.j2",     # tts.py 가 이 틀의 모양을 읽는다
    "rebrief/templates/script_longform.md.j2",
    "rebrief/templates/site_images.html.j2",
    "rebrief/templates/site_search.html.j2",
    "tests/test_shared_files.py",
]


def _sibling() -> Path | None:
    env = os.environ.get("REBRIEF_SIBLING")
    if env:
        return Path(env)
    other = PAIR.get(ROOT.name)
    return ROOT.parent / other if other else None


def test_shared_list_names_real_files():
    missing = [rel for rel in SHARED if not (ROOT / rel).exists()]
    assert not missing, f"목록에 있는데 이 저장소에 없는 파일: {missing}"


def test_shared_files_match_the_sibling_repo():
    other = _sibling()
    if not other or not (other / "rebrief").is_dir():
        pytest.skip("옆 저장소를 찾을 수 없습니다 (REBRIEF_SIBLING 또는 같은 폴더의 옆자리)")
    missing = [rel for rel in SHARED if not (other / rel).exists()]
    assert not missing, f"옆 저장소({other.name})에 없는 공통 파일: {missing}"
    differ = [rel for rel in SHARED if (ROOT / rel).read_bytes() != (other / rel).read_bytes()]
    assert not differ, (
        f"두 저장소에서 달라진 공통 파일: {', '.join(differ)} — 고친 쪽 파일을 {other.name} 에도 "
        "똑같이 복사하세요. 일부러 다르게 둘 거면 두 저장소의 tests/test_shared_files.py 목록에서 빼세요.")
