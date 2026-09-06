@echo off
chcp 65001 >nul
rem Windows 설치 스크립트.  파일을 더블클릭하거나 명령 프롬프트에서:  setup.bat
cd /d "%~dp0"

echo ▶ 파이썬을 찾는 중...

set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo ✗ 파이썬을 찾지 못했습니다.
  echo.
  echo   https://www.python.org/downloads/ 에서 내려받아 설치하세요.
  echo   설치 화면에서 "Add python.exe to PATH" 를 반드시 체크해야 합니다.
  echo.
  echo   참고: 명령 프롬프트에 python 을 쳤을 때 Microsoft Store 가 열리면
  echo         아직 설치가 안 된 상태입니다.
  echo.
  pause
  exit /b 1
)

echo   찾았습니다: %PY%

echo ▶ 가상환경(.venv)을 만드는 중...
%PY% -m venv .venv
if errorlevel 1 goto fail

echo ▶ 패키지를 설치하는 중... (1~2분 걸립니다)
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if errorlevel 1 goto fail

echo ▶ RSS 피드 상태를 확인합니다...
echo.
.venv\Scripts\python.exe -m rebrief doctor

echo.
echo ────────────────────────────────────────────
echo 설치가 끝났습니다.
echo.
echo 다음부터는 명령 프롬프트에서 이렇게 실행하세요.
echo.
echo   cd /d "%~dp0"
echo   .venv\Scripts\activate
echo   python -m rebrief run
echo.
echo API 키를 쓰려면 .env.example 을 .env 로 복사하고 키를 넣으세요.
echo ────────────────────────────────────────────
pause
exit /b 0

:fail
echo.
echo ✗ 설치 중 오류가 발생했습니다. 위 메시지를 확인해 주세요.
pause
exit /b 1
