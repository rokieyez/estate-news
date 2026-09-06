# 부동산 블로그·영상 자동화

매일 아침 부동산 뉴스를 모아서 **블로그 글 한 편**과 **영상 대본 두 개(쇼츠·롱폼)** 를 만들어 둡니다.
일어나서 확인만 하면 그날 찍을 게 준비되어 있는 상태를 목표로 합니다.

```
RSS 수집 ──▶ 중복 제거 ──▶ 이슈로 묶기 ──▶ 점수 매겨 상위 5개 선정
                                                  │
                                                  ▼
                                          Claude 로 사실 정리
                                                  │
                            ┌─────────────────────┼─────────────────────┐
                            ▼                     ▼                     ▼
                        블로그 글            쇼츠 대본 + SRT         롱폼 대본
                                                                   + 제작 메모
```

👉 **[예시 산출물 보기](docs/sample-output/)** (가짜 데이터로 만든 샘플)

---

## 무엇이 나오나

하루치 결과는 `output/2026-09-06/` 처럼 날짜 폴더에 쌓입니다.

| 파일 | 용도 |
| --- | --- |
| `brief.md` | 오늘의 사실 정리. 이슈별 무슨 일 / 숫자 표 / 왜 중요한지 / **확인 필요 지점** |
| `blog.md` | 바로 올릴 수 있는 블로그 글. YAML 머리말 포함 (Jekyll·Hugo·옵시디언 호환) |
| `script-shorts.md` | 60초 쇼츠 대본. 자막 단위로 끊고 컷마다 **화면 지시**가 붙습니다 |
| `script-shorts.srt` | 위 대본의 자막 파일. 프리미어·파이널컷·캡컷에 그대로 임포트 |
| `script-longform.md` | 8분 롱폼 대본. 챕터·타임코드·B-roll·그래픽 지시 포함 |
| `production-notes.md` | 제목/썸네일 문구 후보, 설명란·태그·고정 댓글 복사용, 촬영 체크리스트 |
| `data.json` | 기사에서 뽑은 수치만 모은 것. 차트 만들 때 쓰세요 |
| `sources.md` | 근거 기사 원문 링크 전체 |
| `raw/articles.json` | 수집 원본. 이걸로 언제든 재생성할 수 있습니다 |

대본 분량은 목표 길이에 맞춰 글자 수로 계산합니다. 기본값은 분당 330자(한국어 낭독 속도)이고,
말이 빠르거나 느리면 `config/settings.yaml` 의 `speaking_rate_cpm` 을 조절하세요.

---

## 빠른 시작

```bash
git clone https://github.com/rokieyez/rokiz.git
cd rokiz

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) 피드가 살아있는지 먼저 확인
python -m rebrief doctor

# 2) API 키 없이 수집·정리만 해보기
python -m rebrief run --no-llm

# 3) 요약·대본까지 만들기
cp .env.example .env      # .env 에 ANTHROPIC_API_KEY 입력
python -m rebrief run
```

API 키는 [console.anthropic.com](https://console.anthropic.com) 에서 발급받습니다.

**키가 없어도 됩니다.** 수집·중복제거·이슈 선정까지는 그대로 돌아가고,
요약과 대본은 `prompt-pack.md` 라는 파일로 나옵니다. 그걸 챗봇에 붙여넣으면 같은 결과를 얻습니다.

---

## 매일 아침 자동 실행

GitHub Actions 로 **매일 오전 7시(KST)** 자동 실행되도록 이미 설정돼 있습니다.
저장소에 API 키만 넣어 주면 끝입니다.

1. 저장소 → **Settings** → **Secrets and variables** → **Actions**
2. **New repository secret**
   - Name: `ANTHROPIC_API_KEY`
   - Secret: 발급받은 키
3. **Settings** → **Actions** → **General** → Workflow permissions →
   **Read and write permissions** 체크 (결과를 저장소에 커밋해야 합니다)

시간을 바꾸려면 `.github/workflows/daily-brief.yml` 의 cron 을 고칩니다.
UTC 기준이라 **한국 시간에서 9시간을 뺀 값**을 씁니다.

```yaml
- cron: "0 22 * * *"   # 22:00 UTC = 오전 7시 KST
- cron: "0 20 * * *"   # 20:00 UTC = 오전 5시 KST
```

수동으로 돌려보려면 **Actions** 탭 → *부동산 데일리 브리핑* → **Run workflow**.

---

## 내 채널에 맞게 고치기

품질을 좌우하는 건 `config/settings.yaml` 의 `video` 섹션입니다. 여기부터 손보세요.

```yaml
video:
  channel_name: "부동산 브리핑"
  audience: "내 집 마련을 준비 중이거나 보유 부동산의 가치 변화를 챙기는 30~50대"
  tone: "차분하고 단정한 정보 전달. 과장·단정·투자 권유 금지."
  persona: "부동산 뉴스를 해설하는 진행자. 어려운 용어는 반드시 한 번 풀어서 설명한다."
  shorts_seconds: 60         # 쇼츠 목표 길이
  longform_minutes: 8        # 롱폼 목표 길이
  speaking_rate_cpm: 330     # 본인 낭독 속도(분당 글자 수)
  banned_phrases:            # 대본에 절대 안 쓸 표현
    - "무조건 오른다"
    - "지금 안 사면 늦는다"
  cta: "도움이 되셨다면 구독과 알림 설정 부탁드립니다."
```

`audience` 와 `tone` 은 대본 문체에 그대로 반영됩니다. 구체적으로 쓸수록 결과가 좋아집니다.

다루는 주제를 바꾸려면 `config/sources.yaml` 의 `keywords` 를 조절합니다.
`weight` 가 높은 카테고리가 이슈 선정에서 우선합니다. 예를 들어 재건축 채널이라면
`공급·정비사업` 의 weight 를 3.5 정도로 올리세요.

---

## 뉴스 소스 관리

기본 소스는 **구글뉴스 키워드 피드 7개**(안정적)와 **언론사 부동산 섹션 피드 6개**입니다.
언론사 RSS 주소는 개편 때 자주 바뀌므로, 가끔 점검해 주세요.

```bash
python -m rebrief doctor
```

```
  ✅  gnews_realestate      42건   구글뉴스·부동산
  ❌  mk_realestate         HTTP 404   ...

실패한 피드는 config/sources.yaml 에서 다음처럼 꺼두세요:
  - id: mk_realestate  →  enabled: false
```

언론사 직접 피드는 **본문까지 긁어오기 때문에** 요약 품질이 더 좋습니다.
구글뉴스 링크는 리다이렉트 페이지라 본문 수집을 건너뜁니다(제목·요약만 사용).
가능하면 언론사 피드를 살려 두세요.

---

## 비용

하루 한 번 실행 기준, Claude 호출은 3번입니다(사실 정리 → 블로그 → 대본).
2·3번째 호출은 첫 호출의 입력을 캐시로 재사용합니다.

| 모델 | 하루 | 한 달(30일) |
| --- | --- | --- |
| `claude-opus-5` (기본) | 약 $0.4 | 약 $12 |
| `claude-sonnet-5` | 약 $0.17 | 약 $5 |

실행할 때마다 실제 사용량과 추정 비용이 출력됩니다.

```
💰 claude-opus-5 · 3회 호출 · 입력 40,000 (캐시읽기 20,000) / 출력 9,000 토큰 · 약 $0.435
```

더 저렴하게 쓰려면 `config/settings.yaml` 에서 바꿉니다.

```yaml
llm:
  model: claude-sonnet-5
  effort: medium      # low | medium | high | xhigh | max
```

---

## 명령어

| 명령 | 설명 |
| --- | --- |
| `python -m rebrief run` | 전체 실행 (수집 → 요약 → 대본) |
| `python -m rebrief run --no-llm` | 수집·정리까지만. API 호출 없음 |
| `python -m rebrief run --limit 20` | 기사 20건만. 테스트용 |
| `python -m rebrief collect` | 수집만 하고 원본 저장 |
| `python -m rebrief render --date 2026-09-06` | 저장된 원본으로 산출물만 재생성 |
| `python -m rebrief doctor` | 피드 상태 점검 |

공통 옵션: `-v` (상세 로그), `--config <폴더>` (설정 경로 지정)

대본이 마음에 안 들면 `settings.yaml` 을 고치고 `render` 로 다시 만드세요.
기사를 다시 긁지 않으니 빠르고, 그날의 수집 결과는 그대로 유지됩니다.

---

## 구조

```
rebrief/
  collect.py     RSS 수집, 제목 정규화, 중복 제거, 본문 추출
  cluster.py     같은 사건 묶기 (글자 2-gram Dice 유사도)
  rank.py        이슈 점수 (보도량·키워드·최신성·매체·속보)
  llm.py         Claude 호출. 구조화 출력으로 Pydantic 모델 직접 수신
  prompts.py     프롬프트 3종 + 키 없을 때 쓰는 프롬프트 팩
  render.py      마크다운·SRT·JSON 생성
  pipeline.py    전체 조립
  cli.py         명령줄
  templates/     산출물 서식 (여기만 고쳐도 결과 형식이 바뀝니다)
config/
  sources.yaml   피드 목록 + 키워드 사전
  settings.yaml  파이프라인·영상·블로그 설정
```

같은 기사를 이틀 연속 다루지 않도록 `state/seen.json` 에 이력을 남깁니다
(기본 3일, `run.skip_recent_days` 로 조절).

---

## 알아 둘 것

- **수치는 반드시 원문과 대조하세요.** 프롬프트에서 "자료에 있는 내용만 쓰라"고 강하게 묶어 두었지만,
  숫자를 그대로 방송에 내보내기 전 확인하는 습관이 안전합니다. `production-notes.md` 4절에
  모델이 스스로 표시한 "확인 필요" 항목이 모여 있습니다.
- **RSS 는 제목과 요약만 주는 경우가 많습니다.** 본문 수집이 막힌 매체는 요약 근거가 얕아집니다.
  `doctor` 로 언론사 직접 피드를 최대한 살려 두는 게 품질에 가장 크게 기여합니다.
- **이슈 묶기는 완벽하지 않습니다.** 같은 지역·자산을 다룬 다른 기사가 한 이슈로 묶일 수 있습니다.
  자주 그러면 `cluster.similarity_threshold` 를 0.45 정도로 올리세요 (근거는 `cluster.py` 주석 참고).
- 투자 자문이 아닙니다. 생성물에는 면책 문구가 자동으로 붙습니다.

---

## 테스트

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

네트워크 없이 픽스처로 전 구간을 검증합니다. 템플릿은 `StrictUndefined` 라
변수 이름을 하나만 틀려도 테스트에서 바로 잡힙니다.
