"""모델 출력이 HTML 로 바뀔 때 실행 가능한 조각을 지운다.

기사 본문에 누가 스크립트를 심고 모델이 그걸 글에 그대로 옮기면, 마크다운 변환기는 HTML 을
통과시키므로 사이트(rokiz.net)와 네이버 붙여넣기 페이지에서 실행될 수 있다. 정규식 수준의
단순 정화지만 글은 모델이 쓴 마크다운이라 이 정도면 충분하다. 표·굵게·목록은 건드리지 않는다.
"""

from __future__ import annotations

import re

_DANGEROUS_BLOCKS = re.compile(
    r"<(script|style|iframe|object|embed|form|template|noscript)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS_TAGS = re.compile(
    r"</?(script|style|iframe|object|embed|form|template|noscript|meta|link|base|input|button)\b[^>]*>",
    re.IGNORECASE,
)
_ON_ATTR = re.compile(r"""\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_BAD_URL = re.compile(
    r"""(\s(?:href|src|xlink:href|formaction)\s*=\s*)(["']?)\s*(?:javascript|vbscript|data)\s*:[^"'\s>]*""",
    re.IGNORECASE,
)


def clean_html(html: str) -> str:
    """스크립트·인라인 이벤트·javascript: 주소를 지운다. 나머지 서식은 그대로."""
    if not html or "<" not in html:
        return html
    html = _DANGEROUS_BLOCKS.sub("", html)
    html = _DANGEROUS_TAGS.sub("", html)
    html = _ON_ATTR.sub("", html)
    html = _BAD_URL.sub(lambda m: f'{m.group(1)}{m.group(2)}#', html)
    return html
