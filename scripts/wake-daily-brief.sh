#!/bin/bash
# 아침 브리핑을 밖에서 깨운다 (맥의 launchd 가 부른다).
#
# 왜 있나: 이 저장소는 **깃허브 예약(schedule)이 통째로 안 돕니다.** 2026-09-08 에
# `*/5 * * * *` 탐침을 20분 돌렸는데 0회였고, 같은 워크플로를 손으로 부르면 6초 만에
# 성공했습니다. 워크플로는 active, 저장소는 공개·비포크·비보관, Actions 정상,
# 비활성 안내 띠도 없었습니다. 원인을 못 찾아 밖에서 깨웁니다.
#
# 안전합니다 — 워크플로 첫 단계가 "오늘 산출물이 이미 있으면 건너뜀" 이라
# 깃허브 예약이 되살아나 겹쳐도 두 번 만들지 않습니다.
#
# 새 인증키가 필요 없습니다. gh 가 이미 로그인돼 있고 키체인에서 읽습니다
# (빈 환경에서도 되는 것을 확인했습니다).
set -uo pipefail

GH=/opt/homebrew/bin/gh
REPO=rokieyez/estate-news
LOG="$HOME/Library/Logs/estate-news-wake.log"

mkdir -p "$(dirname "$LOG")"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 잠에서 막 깬 참이면 네트워크가 아직 안 붙어 있을 수 있다. 최대 5분 기다린다.
for i in $(seq 1 10); do
    if /usr/bin/curl -sf -m 10 -o /dev/null https://api.github.com; then break; fi
    say "네트워크를 기다립니다 ($i/10)"
    sleep 30
done

if out=$("$GH" workflow run daily-brief.yml --repo "$REPO" --ref main 2>&1); then
    say "깨웠습니다"
    exit 0
fi
say "실패: $out"
exit 1
