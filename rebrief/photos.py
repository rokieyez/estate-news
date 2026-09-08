"""카드뉴스 위쪽 띠에 얹을 사진 찾기.

**왜 사진을 밖에서 받아 오나.** 기사 사진은 저작권이 있어 쓸 수 없고 수집하지도 않습니다.
그렇다고 인포그래픽만 얹으면 표지가 매일 표처럼 보입니다. 상업 이용이 허락된 무료 사진을
받아 얹으면 첫 장이 사진, 그 뒤가 자료가 되어 묶음에 결이 생깁니다.

**Pexels 를 씁니다** (2026-09-08, 사용자 선택). 인증키가 있어야 하지만 이메일만 넣으면
바로 나오고(심사 없음), 표기 의무가 없으며 사진이 많습니다. 인증키 없이 되는 Openverse 도
재 봤는데 '서울 아파트' 로 상업 이용 가능한 사진이 **일곱 장**뿐이라 며칠이면 같은 사진이
돌아옵니다. 그래도 표기는 답니다 — 의무라서가 아니라 어디서 온 사진인지 밝히는 편이 낫고,
같은 자리에 '본문과 무관' 을 함께 적어야 하기 때문입니다.

**받은 사진은 저장소에 올리지 않습니다.** 원본이 한 장에 3~6MB 라 매일 커밋하면 한 해에
1.8GB 씩 붑니다. `state/photos/` 에 받아 두고(gitignore) 카드 PNG 에만 담습니다.
쓴 사진 번호만 `state/photos.json` 에 남겨 며칠 안에 같은 사진이 다시 나오지 않게 합니다.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PEXELS_SEARCH = "https://api.pexels.com/v1/search"

# 이슈의 성격에 따라 찾을 말을 바꿉니다. Pexels 는 영어로 찾아야 결과가 많이 나옵니다.
# 한국어로 넣으면 몇 건 안 나오는 것을 확인하고 영어 낱말로 짝지어 두었습니다.
QUERY_MAP: list[tuple[tuple[str, ...], str]] = [
    (("재건축", "재개발", "정비사업", "공사비", "착공"), "apartment construction site city"),
    (("전세", "월세", "임대", "보증금"), "apartment living room interior"),
    (("분양", "청약", "미분양", "입주"), "new apartment building exterior"),
    (("대출", "금리", "이자", "주담대", "은행"), "bank finance calculator desk"),
    (("정책", "규제", "정부", "국토부", "세금", "종부세"), "seoul city skyline government"),
    (("거래", "매매", "시세", "집값", "가격"), "seoul apartment buildings"),
]
DEFAULT_QUERY = "seoul apartment buildings"


@dataclass
class Photo:
    path: Path
    credit: str          # 촬영자
    source: str          # 어디서 왔는지 (Pexels)
    ident: str           # 되풀이를 막으려고 남기는 사진 번호


def queries_for(brief: dict, limit: int = 3) -> list[str]:
    """브리핑에서 찾을 말을 뽑는다. 겹치지 않게, 모자라면 기본값으로 채운다."""
    text_by_issue = [f"{i.get('title', '')} {i.get('category', '')} {i.get('one_liner', '')}"
                     for i in (brief.get("issues") or [])]
    out: list[str] = []
    for text in [str(brief.get("headline", ""))] + text_by_issue:
        for words, query in QUERY_MAP:
            if any(w in text for w in words) and query not in out:
                out.append(query)
                break
        if len(out) >= limit:
            break
    while len(out) < limit:
        out.append(DEFAULT_QUERY)
    return out[:limit]


def _get(url: str, headers: dict, timeout: int = 20) -> bytes:
    """망 호출 한 곳. 시험에서 이 함수만 갈아끼우면 네트워크 없이 검증된다."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _pexels(query: str, key: str, *, per_page: int = 15) -> list[dict]:
    url = f"{PEXELS_SEARCH}?" + urllib.parse.urlencode({
        "query": query, "per_page": per_page,
        "orientation": "landscape",     # 띠가 가로로 길다. 세로 사진은 잘려 못 쓴다.
        "size": "medium",               # 원본은 6MB 까지 간다. 띠는 1080px 이면 충분하다.
    })
    data = json.loads(_get(url, {"Authorization": key}).decode("utf-8"))
    return list(data.get("photos") or [])


def fetch(brief: dict, *, cache_dir: Path, ledger: Path, count: int = 3,
          key: str | None = None, keep_recent: int = 40) -> list[Photo]:
    """오늘 쓸 사진 몇 장. 키가 없거나 망이 막히면 빈 목록 — 그날은 인포그래픽으로 간다.

    **여기서 터져도 하루 실행이 죽으면 안 됩니다.** 사진은 있으면 좋은 것이지
    없으면 안 되는 것이 아닙니다. 모든 실패를 로그만 남기고 삼킵니다.
    """
    key = key or os.environ.get("PEXELS_API_KEY", "")
    if not key or count <= 0:
        return []
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        used = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else []
    except (OSError, ValueError):
        used = []
    seen = set(used)

    out: list[Photo] = []
    for query in queries_for(brief, count):
        if len(out) >= count:
            break
        try:
            hits = _pexels(query, key)
        except Exception as exc:              # 망·인증키·응답 형식 무엇이든
            log.warning("사진 검색 실패(%s): %s", query, exc)
            continue
        for hit in hits:
            ident = str(hit.get("id") or "")
            if not ident or ident in seen:
                continue
            src = (hit.get("src") or {}).get("landscape") or (hit.get("src") or {}).get("large")
            if not src:
                continue
            path = cache_dir / f"photo-{ident}.jpg"
            try:
                if not path.exists():
                    path.write_bytes(_get(src, {}))
            except Exception as exc:
                log.warning("사진 내려받기 실패(%s): %s", ident, exc)
                continue
            seen.add(ident)
            used.append(ident)
            out.append(Photo(path=path, credit=str(hit.get("photographer") or "").strip(),
                             source="Pexels", ident=ident))
            break                              # 한 낱말에 한 장씩. 다음 낱말로 넘어간다.

    if out:
        try:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(json.dumps(used[-keep_recent:], ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("사진 장부를 남기지 못했습니다: %s", exc)
    return out
