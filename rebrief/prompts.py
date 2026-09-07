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


def build_blog_user(cfg: Config, regions: list[str] | None = None) -> str:
    blog = cfg.get("blog", {}) or {}
    if str(blog.get("platform", "naver")).lower() == "naver":
        return _blog_user_naver(cfg, blog, regions or [])
    return _blog_user_markdown(blog)


def _blog_user_markdown(blog: dict) -> str:
    min_chars = int(blog.get("min_chars", 1800))
    max_chars = int(blog.get("max_chars", 3500))

    return f"""위 브리핑을 바탕으로 블로그 글 한 편을 완성하세요.

- 분량: 본문 {min_chars}~{max_chars}자
- 구조: 도입(오늘 시장 한 문단) → 이슈별 H2 소제목 → 정리/체크포인트
- 수치는 표(마크다운 테이블)로 정리하면 읽기 좋습니다. 수치가 2개 이상인 이슈는 표를 쓰세요.
- 각 이슈 끝에 근거 기사 링크를 넣습니다.
- 마지막에 '오늘의 체크포인트' 3줄 요약을 붙입니다.
- title 은 검색해서 들어올 만한 제목으로 짓되, 과장하거나 낚지 않습니다.
- 글 안에서 독자를 '여러분'으로 부르고, 존댓말로 씁니다.
- tags 는 5~8개. image_slots 는 빈 배열로 두세요."""


def _blog_user_naver(cfg: Config, blog: dict, regions: list[str] | None = None) -> str:
    """네이버 블로그는 검색 유입과 모바일 열람 비중이 커서 요구사항이 다르다.

    - 검색으로 들어온 사람은 스크롤을 거의 안 한다 → 결론을 맨 앞에
    - 대부분 휴대폰으로 본다 → 문단이 길면 읽지 않는다
    - 본문에 쓴 #해시태그가 그대로 블로그 태그로 등록된다
    """
    min_chars = int(blog.get("min_chars", 1800))
    max_chars = int(blog.get("max_chars", 3500))
    naver = blog.get("naver", {}) or {}
    tag_count = int(naver.get("tag_count", 20))
    image_slots = int(naver.get("image_slots", 3))
    para_max = int(naver.get("paragraph_max_chars", 120))

    region_rule = ""
    if regions:
        names = " · ".join(regions[:5])
        region_rule = f"""

■ 지역명 ({names})
- 부동산 검색은 절반이 지역입니다. 위 지역 가운데 오늘 이슈와 **실제로 관계있는 곳**을
  제목이나 소제목에 넣으세요. 관계없는 지역을 끼워 넣지는 마세요.
- 가능하면 focus_keyword 에도 지역을 넣습니다. 예: "강북구 종부세", "노원구 아파트값".
- tags 에도 지역명을 넣습니다(프로그램이 '지역명+아파트' 형태도 자동으로 덧붙입니다)."""

    return f"""위 브리핑을 바탕으로 **네이버 블로그**에 올릴 글 한 편을 완성하세요.
네이버 블로그의 특성에 맞춰야 하므로 아래 규칙을 정확히 지켜 주세요.

■ 대표 검색어 (focus_keyword) — 이 글의 유입을 책임지는 말
- 오늘 이슈 가운데 **사람들이 실제로 검색창에 칠 말** 하나를 고릅니다. 2~4어절.
  좋은 예: "종부세 대상 자치구", "기업형 임대 세제완화". 나쁜 예: "부동산"(너무 넓음), "오늘의 브리핑"(아무도 안 침).
- 고른 말은 **제목 앞쪽 · 첫 문단 100자 안 · 소제목 한 곳 이상**에 글자 그대로 넣습니다.
  변형("종부세 자치구 대상")이 아니라 같은 표기로 넣어야 검색에 걸립니다.

■ 요약 3줄 (summary_lines)
- 본문 맨 앞에 얹습니다. 검색으로 들어온 사람은 이것만 보고 나가기도 합니다.
- 각 45자 내외. "무슨 일 / 숫자로 얼마 / 그래서 뭘 보면 되는지" 순서.

■ 마무리 질문 (closing_question)
- 글 끝에 붙일 물음 한 문장. 독자가 자기 상황을 떠올리게 하는 질문이 좋습니다.
- "댓글 부탁드립니다" 같은 구걸은 쓰지 않습니다.

■ 제목 (title)
- 위에서 고른 focus_keyword 를 **앞쪽에** 배치합니다. 검색 노출에 유리합니다.
- 25~35자. 날짜를 넣으면 좋습니다. 예: "서울 아파트값 3주 연속 하락, 9월 6일 부동산 브리핑"
- 과장·낚시성 표현은 쓰지 않습니다.

■ 첫 문단 (본문 맨 앞)
- 검색으로 들어온 사람은 스크롤하지 않습니다. **결론부터** 씁니다.
- 3줄 이내로 "오늘 무슨 일이 있었고, 그래서 어떻다"를 끝냅니다.

■ 그래서 나는? (takeaways)
- 2~3개. "무주택 실수요자라면", "1주택자라면", "전세 계약을 앞뒀다면" 처럼 **읽는 사람 유형**으로 시작합니다.
- 각 45자 내외로, 오늘 소식이 그 사람에게 뜻하는 바나 확인할 것을 한 줄로 적습니다.
- 투자를 권하거나 단정하지 않습니다. "~일 수 있습니다", "~부터 확인해 보세요" 정도로.

■ 본문 (body_markdown) — **짧게 쓰는 것이 핵심입니다**
- 분량 {min_chars}~{max_chars}자. **넘기지 마세요.** 길면 아무도 끝까지 읽지 않습니다.
- 오늘 이슈를 전부 길게 다루지 마세요. **가장 중요한 이슈 하나만** 깊게 씁니다.
- 순서를 이렇게 잡습니다:
  1) 도입 2~3문장 — 결론부터
  2) 메인 이슈 `##` 소제목 2~3개 — 무슨 일 → 숫자 → 그래서 어떤 뜻인지
  3) `## 그 밖의 오늘 소식` — 나머지 이슈를 **한 줄씩** 불릿으로. 한 줄 45자 이내, 설명하지 말고 사실만.
  4) `## 오늘의 체크포인트` — 3줄 요약
- **한 문단은 2~3문장, {para_max}자 이내.** 휴대폰 화면에서 벽처럼 보이면 읽지 않습니다.
- 문단 사이는 반드시 빈 줄로 띄웁니다.
- 어려운 말은 **처음 나올 때 괄호로 짧게** 풉니다. 예: 종부세(공시가격 합이 기준을 넘으면 내는 세금).
  배경지식이 없는 사람이 첫 문단에서 막히면 그대로 나갑니다.
- `##` 소제목 가운데 **한 곳 이상에 focus_keyword 를 그대로** 씁니다.
- focus_keyword 를 본문 전체에 3~5회 자연스럽게 반복합니다. 억지로 끼워 넣지는 마세요.
- 소제목은 검색어처럼 씁니다. "정리" 보다 "종부세 대상 자치구는 어디인가" 가 낫습니다.
- 어제 글과 같은 문장을 쓰지 마세요. 표현을 매일 바꿔야 검색에서 중복으로 취급되지 않습니다.
- 수치가 2개 이상인 이슈는 마크다운 표로 정리합니다.
- 이미지 자리를 본문 흐름에 맞게 {image_slots}곳 넣습니다. 형식은 정확히 이렇게 씁니다:
  `[이미지: 어떤 이미지를 넣을지 설명]`
  같은 순서로 image_slots 배열에 담되, 그 자리가 **브리핑의 수치를 그림으로 보여주는 자리**라면
  datapoint_label 에 그 수치의 label 을 **글자 그대로** 적습니다(프로그램이 그 수치로 그림을
  자동 생성해 자리에 넣습니다). 현장 사진·화면 캡처처럼 수치가 아닌 자리는 빈 문자열로 둡니다.
  {image_slots}곳 중 적어도 한 곳은 수치 자리로 잡으세요.
  사진 자리에는 search_keywords 에 스톡 사진 사이트용 **영어 검색어** 2~4단어를 적습니다.
- 각 이슈 끝에 근거 기사 링크를 붙입니다.
- 표는 꼭 필요할 때 하나만 씁니다. 휴대폰에서 표는 가로로 잘립니다.
- 독자를 '여러분'으로 부르고 존댓말로 씁니다. 딱딱한 보고서 문체는 피합니다.

■ 태그 (tags)
- {tag_count}개. 본문 하단에 해시태그로 붙일 것이며 네이버가 이를 태그로 인식합니다.
- 넓은 키워드(부동산, 아파트)와 좁은 키워드(서울아파트값, 전세사기지원)를 섞습니다.
- 띄어쓰기 없이 붙여 씁니다. # 기호는 빼고 단어만 담으세요.{region_rule}"""


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


# ── 주간 결산 ────────────────────────────────────────────────


def build_weekly_messages(cfg: Config, days: list[dict], week_label: str) -> tuple[str, str]:
    """일주일치 data.json 을 묶어 결산 글을 부탁한다. 하루치 브리핑보다 압축해서 넘긴다."""
    video = cfg.get("video", {}) or {}
    blog = cfg.get("blog", {}) or {}
    banned = video.get("banned_phrases", []) or []
    naver = blog.get("naver", {}) or {}
    tag_count = int(naver.get("tag_count", 20))
    min_chars = int(blog.get("weekly_min_chars", 1500))
    max_chars = int(blog.get("weekly_max_chars", 3000))

    compact = [
        {
            "date": d["date"],
            "headline": d.get("headline", ""),
            "market_temperature": d.get("market_temperature", ""),
            "issues": [
                {"title": i.get("title"), "category": i.get("category"),
                 "one_liner": i.get("one_liner"), "numbers": i.get("numbers", []),
                 **({"days_seen": i["days_seen"]} if i.get("days_seen") else {})}
                for i in d.get("issues", [])
            ],
        }
        for d in days
    ]
    streaks = [s for d in days for s in d.get("streaks", [])]
    streak_note = ""
    if streaks:
        lines_ = "\n".join(f"- {s['title']} — {len(s['dates'])}일 ({', '.join(s['dates'])})" for s in streaks)
        streak_note = f"""

여러 날 반복된 이슈 (같은 사건이 날짜만 바뀌어 다시 나온 것입니다. 각각 세지 말고 흐름으로 묶으세요):
{lines_}"""
    system = f"""당신은 부동산 콘텐츠를 만드는 프로듀서입니다.
채널명은 "{video.get('channel_name', '부동산 브리핑')}" 입니다.

시청자: {video.get('audience', '부동산에 관심 있는 일반 시청자')}
톤앤매너: {video.get('tone', '차분하고 정확한 정보 전달')}

지켜야 할 것:
- 아래 일주일치 브리핑에 있는 사실과 수치만 씁니다. 없는 내용을 채워 넣지 마세요.
- 다음 표현은 쓰지 마세요: {', '.join(banned) if banned else '(없음)'}
- 단정적 예측 대신 근거와 전망 주체를 밝힙니다.
- 모든 출력은 한국어입니다.

────────── {week_label} 브리핑 모음 (JSON, 날짜순) ──────────
{json.dumps(compact, ensure_ascii=False, indent=1)}
──────────────────────────────────────{streak_note}"""

    user = f"""위 일주일치 브리핑으로 **주간 결산 글** 한 편을 완성하세요. 네이버 블로그에 올립니다.

■ five_lines
- 이번 주를 다섯 줄로 요약합니다. 각 줄 40자 이내, 가능하면 숫자를 넣습니다.
- 요일 순이 아니라 **중요한 순**입니다.

■ 본문 (body_markdown)
- 분량 {min_chars}~{max_chars}자. 한 문단 2~3문장, 문단 사이 빈 줄.
- 첫 문단에 결론(이번 주 시장을 한 문장으로)을 씁니다.
- `##` 소제목 3~5개. **날짜별이 아니라 주제별**로 묶습니다. 같은 이슈가 여러 날 나왔으면
  흐름(무엇이 바뀌었는지)을 짚습니다.
- 수치가 2개 이상인 주제는 마크다운 표로 정리합니다. 표에는 날짜 열을 둡니다.
- 마지막 소제목은 '다음 주 볼 것'으로 하고 next_week_watch 와 같은 내용을 넣습니다.
- 독자를 '여러분'으로 부르고 존댓말로 씁니다.

■ 태그 (tags)
- {tag_count}개. 띄어쓰기 없이, # 기호 없이 단어만."""
    return system, user


def build_weekly_prompt_pack(cfg: Config, days: list[dict], week_label: str) -> str:
    """API 키가 없을 때 챗봇에 붙여넣을 수 있게 두 메시지를 하나의 문서로 묶는다."""
    system, user = build_weekly_messages(cfg, days, week_label)
    return f"""# {week_label} 주간 결산 — 프롬프트 팩

API 키가 없어 자동 생성을 건너뛰었습니다. 아래를 통째로 복사해 챗봇에 붙여넣으면 같은 결과를
얻을 수 있습니다. (WeeklyReview 형식: title, slug, meta_description, five_lines, body_markdown,
next_week_watch, tags)

---

{system}

---

{user}
"""

