"""쇼츠 대본을 일레븐랩스 음성(mp3)으로 — 두 저장소 공통 파일입니다 (2026-09-17 사용자 요청).

  output/2026-09-18/script-shorts_estate-news_260918.mp3   읽은 음성
  output/2026-09-18/voice.json                              무슨 목소리로 몇 자를 읽었는지

대본 페이지(`script-shorts.md`)에서 **읽는 말만** 뽑아(tts.py) 한 번에 읽힙니다. 컷마다 따로 만들면
문장마다 억양이 튑니다 (2026-09-13 커뮤니티 쇼츠에서 확인한 것).

목소리는 세 군데서 정해지고 뒤의 것이 이깁니다:
  1) config/settings.yaml 의 voice.voice_id         기본값
  2) state/voice.json                               「앞으로도 이 목소리로」를 고른 결과
  3) `python -m rebrief voice --voice 이름`          그 한 번만

열쇠는 환경변수 ELEVENLABS_API_KEY. 없으면 조용히 건너뜁니다 — 대본은 그대로 나옵니다.
**열쇠 값은 어디에도 찍지 않습니다.** 같은 글·같은 목소리로 이미 만든 파일이 있으면 다시 부르지 않습니다
(다시 그리기 때마다 글자 수가 나가지 않게).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .tts import say_as, shorts_text, speakable

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io"
KEY_ENV = "ELEVENLABS_API_KEY"
SIDECAR = "voice.json"
SCRIPT = "script-shorts.md"

# 거절 까닭을 사람 말로. 받은 JSON 을 그대로 띄우면 무엇을 해야 할지 알 수 없다.
ERRORS = {
    "quota_exceeded": "이번 달 일레븐랩스 글자 수를 다 썼습니다",
    "invalid_api_key": "일레븐랩스 열쇠가 맞지 않습니다",
    "missing_permissions": "일레븐랩스 열쇠에 권한이 빠져 있습니다 (Text to Speech · Voices 읽기)",
    "voice_not_found": "고른 목소리를 일레븐랩스에서 찾지 못했습니다",
    "detected_unusual_activity": "일레븐랩스가 이 요청을 막았습니다 (무료 요금제는 서버에서 부를 수 없습니다)",
    "too_many_concurrent_requests": "일레븐랩스 요청이 몰렸습니다",
}


@dataclass
class VoiceResult:
    ok: bool = False
    file: str = ""
    voice_id: str = ""
    voice_name: str = ""
    chars: int = 0
    reused: bool = False           # 같은 글·같은 목소리라 다시 부르지 않았다
    remaining: int | None = None   # 이번 달 남은 글자 수 (모르면 None)
    note: str = ""                 # 건너뛰었거나 실패한 까닭


def _post(url: str, **kw):
    """시험에서 갈아끼우는 자리 — requests 전역을 패치하면 수집기 스텁과 부딪힌다."""
    import requests

    return requests.post(url, **kw)


def _get(url: str, **kw):
    import requests

    return requests.get(url, **kw)


def api_key() -> str:
    return (os.environ.get(KEY_ENV) or "").strip()


def options(cfg: Config) -> dict:
    """settings.yaml 의 voice 에 state/voice.json(고른 목소리)을 덮어쓴 값."""
    opts = dict(cfg.get("voice", {}) or {})
    path = cfg.state_dir / SIDECAR
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            saved = {}
        if saved.get("voice_id"):
            opts["voice_id"] = saved["voice_id"]
    return opts


def choices(cfg: Config) -> list[dict]:
    """고를 수 있는 목소리 목록 — settings.yaml 의 voice.voices ({name, id, note, preview})."""
    return [dict(v) for v in ((cfg.get("voice", {}) or {}).get("voices", []) or []) if v.get("id")]


def resolve(cfg: Config, pick: str = "") -> tuple[str, str]:
    """이름이나 voice_id → (voice_id, 이름). 목록에 없는 글자열은 voice_id 로 보고 그대로 쓴다.

    일레븐랩스 목소리 도서관의 목소리는 내 목록에 담지 않아도 voice_id 로 바로 쓸 수 있다.
    """
    pick = (pick or "").strip() or str(options(cfg).get("voice_id", "") or "").strip()
    for v in choices(cfg):
        if pick in (v["id"], v.get("name", "")):
            return v["id"], v.get("name", "") or v["id"]
    return pick, pick


def save_choice(cfg: Config, voice_id: str, name: str = "") -> Path:
    """「앞으로도 이 목소리로」 — state/voice.json 에 남긴다 (설정 파일은 건드리지 않는다)."""
    path = cfg.state_dir / SIDECAR
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"voice_id": voice_id, "name": name,
                                "chosen_at": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def spoken_text(out_dir: Path, spell_out: bool = True, table: dict | None = None) -> str:
    """그날 쇼츠 대본에서 읽는 말만. 기호·단위(㎡·%p·~·→)는 말로 풀어 읽히고,
    table(설정 voice.say_as)의 낱말은 소리 나는 대로 바꾼다 (「신고가」→「신고까」)."""
    path = out_dir / SCRIPT
    if not path.exists():
        return ""
    text = shorts_text(path.read_text(encoding="utf-8"))
    return speakable(text, table) if spell_out else say_as(text, table)


def _fingerprint(text: str, voice_id: str, model: str, speed: float) -> str:
    return hashlib.sha256(f"{voice_id}|{model}|{speed}|{text}".encode("utf-8")).hexdigest()[:16]


def load_sidecar(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / SIDECAR).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def explain(response) -> str:
    """거절 응답 → 한 줄 까닭."""
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    status = detail.get("status", "") if isinstance(detail, dict) else ""
    message = (detail.get("message", "") if isinstance(detail, dict) else str(detail or "")) or response.text[:160]
    if status in ERRORS:
        return f"{ERRORS[status]} (HTTP {response.status_code})"
    return f"일레븐랩스 호출 실패 (HTTP {response.status_code}{' · ' + status if status else ''}): {message[:160]}"


def remaining_chars(key: str) -> int | None:
    """이번 달 남은 글자 수. 조회는 글자 수를 쓰지 않는다. 못 읽으면 None."""
    try:
        r = _get(f"{API}/v1/user/subscription", headers={"xi-api-key": key}, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return int(data["character_limit"]) - int(data["character_count"])
    except Exception:                       # 남은 글자 수는 곁다리 정보다 — 무엇이 터져도 음성은 살린다
        return None


def account_voices(key: str) -> list[dict]:
    """내 일레븐랩스 목록의 목소리 — settings.yaml 의 voice.voices 를 채울 때 쓴다."""
    r = _get(f"{API}/v1/voices", headers={"xi-api-key": key}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(explain(r))
    rows = []
    for v in r.json().get("voices", []):
        labels = v.get("labels") or {}
        rows.append({"id": v.get("voice_id", ""), "name": v.get("name", ""),
                     "language": labels.get("language", ""), "gender": labels.get("gender", ""),
                     "age": labels.get("age", ""), "preview": v.get("preview_url", "")})
    return rows


def make_shorts_voice(cfg: Config, out_dir: Path, filename: str, *, pick: str = "",
                      force: bool = False) -> VoiceResult:
    """그날 쇼츠 대본을 읽혀 out_dir/filename(mp3) 으로 남긴다. 무엇이 터져도 예외를 내지 않는다."""
    opts = options(cfg)
    result = VoiceResult(file=filename)
    if not opts.get("enabled", True):
        result.note = "음성 만들기가 꺼져 있습니다 (voice.enabled)"
        return result
    key = api_key()
    if not key:
        result.note = f"일레븐랩스 열쇠({KEY_ENV})가 없어 음성을 건너뛰었습니다"
        return result
    text = spoken_text(out_dir, bool(opts.get("spell_out", True)), opts.get("say_as") or None)
    if not text:
        result.note = "쇼츠 대본에서 읽을 말을 찾지 못했습니다"
        return result
    limit = int(opts.get("max_chars", 1200) or 0)
    if limit and len(text) > limit:
        # 대본이 비정상으로 길면(틀이 깨져 표 전체가 들어오는 등) 글자 수를 지키는 쪽을 택한다
        result.note = f"읽을 말이 {len(text):,}자라 음성을 만들지 않았습니다 (한도 {limit:,}자 · voice.max_chars)"
        return result

    voice_id, name = resolve(cfg, pick)
    if not voice_id:
        result.note = "고른 목소리가 없습니다 (voice.voice_id)"
        return result
    model = str(opts.get("model", "eleven_multilingual_v2") or "eleven_multilingual_v2")
    speed = float(opts.get("speed", 1.0) or 1.0)
    result.voice_id, result.voice_name, result.chars = voice_id, name, len(text)

    mark = _fingerprint(text, voice_id, model, speed)
    old = load_sidecar(out_dir)
    if not force and old.get("mark") == mark and (out_dir / str(old.get("file", ""))).is_file():
        result.ok, result.reused, result.file = True, True, old["file"]
        return result

    try:
        r = _post(
            f"{API}/v1/text-to-speech/{voice_id}",
            params={"output_format": str(opts.get("output_format", "mp3_44100_128"))},
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            json={"text": text, "model_id": model,
                  "voice_settings": {"stability": float(opts.get("stability", 0.45)),
                                     "similarity_boost": float(opts.get("similarity_boost", 0.75)),
                                     "speed": speed}},
            timeout=float(opts.get("timeout_seconds", 180)),
        )
    except Exception as exc:                 # 망·시간 초과 무엇이든 — 음성 하나 때문에 그날 실행이 죽으면 안 된다
        result.note = f"일레븐랩스에 닿지 못했습니다 ({type(exc).__name__})"
        return result
    if r.status_code != 200:
        result.note = explain(r)
        return result
    if len(r.content) < 1000:
        result.note = "일레븐랩스가 빈 음성을 돌려줬습니다"
        return result

    for stale in out_dir.glob("script-shorts*.mp3"):      # 목소리를 바꿔 다시 만든 날 옛 파일이 남지 않게
        if stale.name != filename:
            stale.unlink()
    (out_dir / filename).write_bytes(r.content)
    result.ok = True
    result.remaining = remaining_chars(key)
    (out_dir / SIDECAR).write_text(json.dumps({
        "file": filename, "voice_id": voice_id, "voice_name": name, "model": model, "speed": speed,
        "chars": len(text), "mark": mark, "bytes": len(r.content),
        "made_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("쇼츠 음성 %s (%s · %d자)", filename, name, len(text))
    return result


def summary_line(result: VoiceResult) -> str:
    """실행 결과·알림에 적을 한 줄. 건너뛴 까닭도 여기서 나온다."""
    if not result.ok:
        return result.note
    left = f" · 이번 달 남은 글자 {result.remaining:,}자" if result.remaining is not None else ""
    if result.reused:
        return f"쇼츠 음성은 같은 글·같은 목소리라 그대로 뒀습니다 ({result.file})"
    return f"쇼츠 음성을 만들었습니다 — {result.voice_name} · {result.chars:,}자{left}"
