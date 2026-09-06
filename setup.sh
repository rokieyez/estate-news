#!/usr/bin/env bash
# macOS / Linux 설치 스크립트.  터미널에서:  bash setup.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "▶ 파이썬을 찾는 중…"

PY=""
for candidate in python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
    major="${version%%.*}"
    minor="${version##*.}"
    if [ "$major" = "3" ] && [ -n "$minor" ] && [ "$minor" -ge 9 ] 2>/dev/null; then
      PY="$candidate"
      echo "  찾았습니다: $candidate (버전 $version)"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  cat <<'MSG'

✗ 파이썬 3.9 이상을 찾지 못했습니다.

  macOS 라면 아래 중 하나로 설치하세요.
    1) https://www.python.org/downloads/  에서 내려받아 설치 (가장 쉬움)
    2) Homebrew 가 있다면:  brew install python@3.12

  설치한 뒤 터미널을 새로 열고 이 스크립트를 다시 실행하세요.

  참고: macOS 에서는 'python' 이 아니라 'python3' 입니다.
        'python' 만 쳤을 때 아무 반응이 없는 건 정상입니다.
MSG
  exit 1
fi

echo "▶ 가상환경(.venv)을 만드는 중…"
"$PY" -m venv .venv

echo "▶ 패키지를 설치하는 중… (1~2분 걸립니다)"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

echo "▶ RSS 피드 상태를 확인합니다…"
echo
./.venv/bin/python -m rebrief doctor || true

PROJECT_DIR="$(pwd)"
cat <<MSG

────────────────────────────────────────────
설치가 끝났습니다.

다음부터는 터미널에서 이렇게 실행하세요.

  cd "$PROJECT_DIR"
  source .venv/bin/activate
  python -m rebrief run

API 키를 쓰려면 .env 파일을 만들고 키를 넣으세요.

  cp .env.example .env
  # .env 를 열어 ANTHROPIC_API_KEY 를 채웁니다
────────────────────────────────────────────
MSG
