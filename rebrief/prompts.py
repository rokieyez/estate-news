"""Claude 에 보낼 프롬프트 조립.

세 번 호출한다.
  1) 수집된 기사 → DailyBrief (사실 정리)
  2) DailyBrief → BlogPost
  3) DailyBrief → VideoPack (쇼츠 + 롱폼 대본)

2·3번은 **같은 system 블록**(페르소나 + 오늘의 브리핑)을 공유한다.
프롬프트 캐싱은 접두사 일치 방식이라, 이렇게 해두면 3번 호출이 2번의
캐시를 그대로 태워 입력 비용이 크게 줄어든다.
"""

from __future__ import annotations

import json

from .config import Config
from .models import Cluster, DailyBrief

# 사실 왜곡이 가장 치명적인 단계라 규칙을 앞에 못 박는다.
ANALYST_SYSTEM = """당신은 한국 부동산 시장을 매일 정리하는 뉴스 애널리스트입니다.

절대 규칙:
1. 아래 제공된 기사 자료에 **적혀 있는 내용만** 사용합니다. 기억이나 추측으로 사실·수치·날짜를 만들어내지 마세요.
2. 수치는 기사에 나온 숫자를 그대로 옮깁니다. 반올림하거나 어림잡지 마세요.
3. 기사 요약만 있고 본문이 없어 내용이 불충분하면, 확인된 만큼만 쓰고 부족한 부분은 caution 에 적으세요.
4. 여러 기사가 서로 다른 숫자를 말하면 둘 다 적고 caution 에 불일치를 명시합니다.
5. 투자 권유·단정적 예측을 하지 않습니다. "오를 것이다"가 아니라 "~라는 전망이 나온다"로 씁니다.
6. 모든 출력은 한국어입니다."""


def build_brief_messages(cfg: Config, clusters: list[Cluster], run_date: str) -> tuple[str, str]:
    """(system, user) 를 돌려준다."""
    material = format_clusters(clusters)
    max_issues = len(clusters)

    user = f"""오늘은 {run_date} 입니다. 아래는 오늘 오전까지 수집한 한국 부동산 관련 기사 {max_issues}개 이슈입니다.

{material}

이 자료만 근거로 오늘의 부동산 데일리 브리핑을 작성하세요.

작성 지침:
- issues 는 제공된 이슈 순서를 유지하되, 자료가 부실해 쓸 내용이 없는 이슈는 빼도 됩니다.
- what_happened 에는 해석이 아니라 확인된 사실만 넣습니다.
- numbers 에는 기사에 등장한 수치를 빠짐없이 담습니다. 영상 자막 카드로 쓸 재료입니다.
- why_it_matters 는 '그래서 시청자에게 무슨 의미인가'를 씁니다. 일반론 말고 이 이슈에 한정해서.
- tomorrow_watch 에는 발표 예정 통계, 회의 일정처럼 **자료에 언급된** 예정 사항만 적습니다. 없으면 빈 배열."""

    return ANALYST_SYSTEM, user


def build_shared_context(cfg: Config, brief: DailyBrief) -> str:
    """블로그·영상 호출이 공유하는 system 블록 (캐시 대상)."""
    video = cfg.get("video", {}) or {}
    banned = video.get("banned_phrases", []) or []

    brief_json = json.dumps(brief.model_dump(), ensure_ascii=False, indent=2)

    return f"""당신은 부동산 콘텐츠를 만드는 프로듀서입니다.
채널명은 "{video.get('channel_name', '부동산 브리핑')}" 입니다.

시청자: {video.get('audience', '부동산에 관심 있는 일반 시청자')}
톤앤매너: {video.get('tone', '차분하고 정확한 정보 전달')}
진행자 캐릭터: {video.get('persona', '부동산 뉴스 해설자')}

지켜야 할 것:
- 아래 브리핑에 있는 사실과 수치만 씁니다. 없는 내용을 채워 넣지 마세요.
- 어려운 용어(DSR, 토지거래허가구역 등)는 처음 나올 때 한 번 짧게 풀어 줍니다.
- 다음 표현은 쓰지 마세요: {', '.join(banned) if banned else '(없음)'}
- 단정적 예측 대신 근거와 전망 주체를 밝힙니다.
- 모든 출력은 한국어입니다.

────────── 오늘의 브리핑 (JSON) ──────────
{brief_json}
──────────────────────────────────────"""


def build_blog_user(cfg: Config) -> str:
    blog = cfg.get("blog", {}) or {}
    min_chars = int(blog.get("min_chars", 1800))
    max_chars = int(blog.get("max_chars", 3500))

    return f"""위 브리핑을 바탕으로 블로그 글 한 편을 완성하세요.

- 분량: 본문 {min_chars}~{max_chars}자
- 구조: 도입(오늘 시장 한 문단) → 이슈별 H2 소제목 → 정리/체크포인트
- 수치는 표(마크다운 테이블)로 정리하면 읽기 좋습니다. 수치가 2개 이상인 이슈는 표를 쓰세요.
- 각 이슈 끝에 근거 기사 링크를 넣습니다.
- 마지막에 '오늘의 체크포인트' 3줄 요약을 붙입니다.
- title 은 검색해서 들어올 만한 제목으로 짓되, 과장하거나 낚지 않습니다.
- 글 안에서 독자를 '여러분'으로 부르고, 존댓말로 씁니다."""


def build_video_user(cfg: Config) -> str:
    video = cfg.get("video", {}) or {}
    shorts_sec = int(video.get("shorts_seconds", 60))
    long_min = float(video.get("longform_minutes", 8))
    cpm = int(video.get("speaking_rate_cpm", 330))
    cta = video.get("cta", "구독과 알림 설정 부탁드립니다.")

    shorts_chars = int(shorts_sec / 60 * cpm)
    long_chars = int(long_min * cpm)

    return f"""위 브리핑을 바탕으로 오늘 촬영할 영상 두 편의 제작 자료를 만드세요.

■ 쇼츠 ({shorts_sec}초)
- 브리핑에서 **가장 임팩트 있는 이슈 하나만** 고릅니다. 여러 개 담지 마세요.
- 발화 분량 합계 약 {shorts_chars}자 (분당 {cpm}자 기준). 이 분량을 넘기지 마세요.
- lines 는 화면에 한 번에 뜨는 자막 단위로 끊습니다. 한 줄 18자 이내.
- at 은 00:00 부터 시작해 실제 읽는 속도에 맞춰 매깁니다.
- visual 에는 그 구간에 무엇을 띄울지 적습니다. 촬영/편집자가 그대로 보고 작업할 수 있게 구체적으로.
  나쁜 예: "관련 화면". 좋은 예: "서울 아파트 단지 항공 스톡 + 좌하단에 '-0.03%' 자막 카드".

■ 롱폼 ({long_min}분)
- 발화 분량 합계 약 {long_chars}자.
- sections 는 4~6개. 각 섹션 at 은 누적 타임코드로 매깁니다.
- script 는 실제로 읽을 원고입니다. 구어체로, 한 문장을 짧게 씁니다. 개조식으로 쓰지 마세요.
- broll 에는 그 구간에 필요한 자료화면을 적습니다. 직접 촬영할 것과 스톡으로 대체할 것을 구분해 주세요.
- graphics 에는 자막 카드나 그래프로 만들 수치·문구를 적습니다. 브리핑 numbers 를 최대한 활용하세요.
- thumbnail_texts 는 썸네일에 크게 박을 문구입니다. 12자 이내, 숫자를 넣으면 좋습니다.
- description 에는 챕터 타임코드 목록과 출처 링크를 포함합니다.
- outro 는 다음 문장으로 마무리합니다: "{cta}"
- pinned_comment 에는 핵심 요약 3줄과 "투자 판단의 책임은 본인에게 있습니다" 취지의 문구를 넣습니다."""


# ── 자료 직렬화 ──────────────────────────────────────────────


def format_clusters(clusters: list[Cluster]) -> str:
    """클러스터를 프롬프트에 넣을 텍스트로 변환."""
    blocks: list[str] = []

    for index, cluster in enumerate(clusters, start=1):
        lead = cluster.lead
        header = (
            f"### 이슈 {index}. {lead.title}\n"
            f"- 보도 매체 {cluster.size}건: {', '.join(cluster.publishers[:8])}\n"
            f"- 분류: {', '.join(cluster.categories) or '미분류'}"
        )

        article_lines: list[str] = []
        for article in cluster.articles[:6]:
            when = article.published.strftime("%m-%d %H:%M") if article.published else "시각미상"
            source = article.publisher or article.feed_name
            text = _truncate(article.best_text, 1200)
            article_lines.append(
                f"* [{source} / {when}] {article.title}\n"
                f"  URL: {article.url}\n"
                f"  내용: {text or '(요약 없음)'}"
            )

        blocks.append(header + "\n" + "\n".join(article_lines))

    return "\n\n".join(blocks)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def build_prompt_pack(cfg: Config, clusters: list[Cluster], run_date: str) -> str:
    """API 키가 없을 때 쓰는 붙여넣기용 프롬프트 문서."""
    system, user = build_brief_messages(cfg, clusters, run_date)
    video = cfg.get("video", {}) or {}

    return f"""# 붙여넣기용 프롬프트 팩 — {run_date}

`ANTHROPIC_API_KEY` 가 없어서 자동 요약을 건너뛰었습니다.
아래 내용을 Claude 나 다른 챗봇에 그대로 붙여넣으면 같은 결과를 얻을 수 있습니다.
(키를 넣고 `python -m rebrief run` 을 다시 돌리면 전부 자동으로 만들어집니다.)

---

## 1단계 — 사실 정리

<details><summary>펼쳐서 전체 복사</summary>

```text
{system}

{user}
```

</details>

---

## 2단계 — 블로그 글

1단계 답변을 붙여넣은 뒤, 이어서 아래를 입력하세요.

```text
{build_blog_user(cfg)}
```

---

## 3단계 — 영상 대본

같은 대화에서 이어서 아래를 입력하세요.

```text
채널명: {video.get('channel_name', '부동산 브리핑')}
시청자: {video.get('audience', '')}
톤앤매너: {video.get('tone', '')}

{build_video_user(cfg)}
```
"""
