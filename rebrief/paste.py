"""붙여넣기 모드 — API 잔액 대신 사람이 claude.ai 에 붙여넣고 답을 되넣는다 (0원).

2026-09-10 사용자 결정: "크레딧 사용량이 부담스럽다". 하루 0.42달러가 한 달 18,000원이라
API 를 끊고, 구독으로 쓰는 claude.ai 에 사람이 직접 붙여넣는 길을 택했습니다.

흐름:
  아침 실행(러너) → 수집·통계·그림까지 만들고, **1단계(사실 정리) 프롬프트를 깃허브 이슈**로 엽니다.
  사람 → 이슈의 상자를 복사해 claude.ai 에 붙여넣고, 답(JSON)을 **이슈 댓글**로 붙여넣습니다.
  워크플로(붙여넣기 반영) → 댓글의 JSON 을 검사해 그 단계 산출물을 만들고, 다음 단계 프롬프트를
  댓글로 답니다. 세 단계가 끝나면 이슈를 닫고 사이트를 배포합니다.

왜 이슈인가: 정적 사이트는 입력을 받을 수 없고, 워크플로 입력 칸은 한 줄짜리라 긴 JSON 을
붙여넣기 어렵습니다. 이슈 댓글은 휴대폰에서도 붙여넣기 쉽고 65,000자까지 받으며, 코드 상자에
복사 버튼이 붙습니다. 댓글은 **저장소 주인만** 반영됩니다 (공개 저장소라 누구나 댓글을 달 수 있음).

`output/<날짜>/paste/` 에 남는 것:
  issues.json   그날 고른 이슈(클러스터). 기사 **본문은 비워서** 저장합니다 — 본문은 커밋하지 않습니다.
  status.json   어느 단계까지 됐는지, 이슈 주소
  brief.json    1단계 답(온전한 DailyBrief). data.json 은 요약본이라 뒤 단계에 못 씁니다.
  keys.json     2단계에서 고른 오늘의 핵심 수치 (3단계 썸네일 배지가 씁니다)
  1-brief.md · 2-blog.md · 3-video.md   단계별 프롬프트 (이슈·댓글에 올린 것과 같음)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import BlogPost, Cluster, DailyBrief, VideoPack

log = logging.getLogger(__name__)

DIR = "paste"
STEPS = ("brief", "blog", "video")
STEP_TITLES = {"brief": "1단계 · 사실 정리", "blog": "2단계 · 블로그 글", "video": "3단계 · 영상 대본"}
LABEL = "붙여넣기"
# 깃허브 이슈 본문·댓글 상한은 65,536자. 넘치면 파일 링크로 대신한다.
BODY_LIMIT = 60_000


# ── 상태 ─────────────────────────────────────────────────────


def paste_dir(out_dir: Path) -> Path:
    return out_dir / DIR


def load_status(out_dir: Path) -> dict | None:
    path = paste_dir(out_dir) / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_status(out_dir: Path, status: dict) -> None:
    status["done"] = all(status.get("steps", {}).get(s) for s in STEPS)
    (paste_dir(out_dir) / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_issues(out_dir: Path) -> list[Cluster]:
    path = paste_dir(out_dir) / "issues.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Cluster.model_validate(item) for item in data]


# ── 프롬프트 ─────────────────────────────────────────────────


def chat_prompt(system: str, user: str, model_cls, title: str) -> str:
    """API 의 (system, user, 구조화 출력) 을 채팅창에 붙여넣을 글 하나로 합친다.

    API 는 스키마를 강제하지만 채팅은 그러지 못하므로 **스키마를 글로 넣고 JSON 하나만**
    답하라고 못 박습니다. 답을 되넣을 때 pydantic 으로 다시 검사합니다.
    """
    schema = json.dumps(model_cls.model_json_schema(), ensure_ascii=False, indent=1)
    return (f"[{title}]\n\n{system}\n\n{user}\n\n---\n"
            "출력 규칙: 설명이나 인사 없이 **JSON 하나만** ```json 상자에 담아 답하세요. "
            "아래 JSON 스키마의 필드 이름과 형식을 그대로 따릅니다. "
            "문자열 안의 줄바꿈은 \\n 으로 적습니다.\n\n"
            f"```json\n{schema}\n```")


def brief_prompt(cfg: Config, issues: list[Cluster], date_str: str) -> str:
    from .prompts import build_brief_messages

    system, user = build_brief_messages(cfg, issues, date_str)
    return chat_prompt(system, user, DailyBrief, STEP_TITLES["brief"])


def blog_prompt(cfg: Config, brief: DailyBrief) -> str:
    from .prompts import build_blog_user, build_shared_context
    from .regions import from_brief

    return chat_prompt(build_shared_context(cfg, brief), build_blog_user(cfg, from_brief(brief)),
                       BlogPost, STEP_TITLES["blog"])


def video_prompt(cfg: Config, brief: DailyBrief, stats: dict | None) -> str:
    from .prompts import build_shared_context, build_video_user

    return chat_prompt(build_shared_context(cfg, brief), build_video_user(cfg, stats),
                       VideoPack, STEP_TITLES["video"])


def _fence(text: str) -> str:
    """프롬프트 안에 ``` 상자가 들어 있으므로 바깥은 백틱 넷으로 감싼다."""
    return f"````text\n{text}\n````"


def _box_or_link(text: str, repo: str, date_str: str, name: str) -> str:
    if len(text) <= BODY_LIMIT:
        return _fence(text)
    link = f"https://raw.githubusercontent.com/{repo}/main/output/{date_str}/{DIR}/{name}" if repo else \
        f"output/{date_str}/{DIR}/{name}"
    return f"프롬프트가 길어 상자에 못 담았습니다. 이 파일을 열어 전체 선택 → 복사하세요: {link}"


HOW_TO = ("**하는 법** — 상자 오른쪽 위 복사 버튼 → claude.ai 새 대화에 붙여넣기 → 답이 나오면 "
          "답 전체를 복사 → **이 이슈에 댓글로 붙여넣기.** 1~2분 뒤 다음 단계 상자가 댓글로 올라옵니다.")


def issue_body(date_str: str, prompt: str, repo: str = "") -> str:
    return (f"## {date_str} 붙여넣기 — {STEP_TITLES['brief']}\n\n{HOW_TO}\n\n"
            f"{_box_or_link(prompt, repo, date_str, '1-brief.md')}\n")


def next_comment(date_str: str, blog: str, video: str, repo: str = "", *, made: str = "") -> str:
    lines = [f"✅ {STEP_TITLES['brief']} 반영했습니다." + (f" {made}" if made else ""),
             "", "이제 아래 **두 상자를 각각** claude.ai 에 붙여넣고, 답 둘을 댓글로 주세요 "
             "(한 댓글에 둘 다 넣어도, 따로 넣어도 됩니다).", "",
             f"### {STEP_TITLES['blog']}", "", _box_or_link(blog, repo, date_str, "2-blog.md"), "",
             f"### {STEP_TITLES['video']}", "", _box_or_link(video, repo, date_str, "3-video.md"), ""]
    return "\n".join(lines)


def remaining_body(out_dir: Path, date_str: str, repo: str, status: dict, why: str = "") -> str:
    """자동 답하기가 중간에 멈춘 날의 이슈 본문 — 아직 안 된 단계의 상자만 담는다."""
    pdir = paste_dir(out_dir)
    steps = status.get("steps") or {}
    head = [f"## {date_str} 붙여넣기 — 자동 답하기가 멈춘 곳부터"]
    if why:
        head += ["", f"자동으로 하다가 멈췄습니다: {why}"]
    done = [STEP_TITLES[s] for s in STEPS if steps.get(s)]
    if done:
        head += ["", "이미 된 단계: " + " · ".join(done)]
    head += ["", HOW_TO, ""]
    files = {"brief": "1-brief.md", "blog": "2-blog.md", "video": "3-video.md"}
    for step in STEPS:
        path = pdir / files[step]
        if steps.get(step) or not path.exists():
            continue
        head += [f"### {STEP_TITLES[step]}", "",
                 _box_or_link(path.read_text(encoding="utf-8"), repo, date_str, files[step]), ""]
    return "\n".join(head)


# ── 구독으로 자동 답하기 (2026-09-11 사용자 결정 — "1번 가자") ──────────
#
# 사람이 claude.ai 에 붙여넣던 세 상자를, 러너 안에서 Claude Code 가 대신 받는다.
# `claude setup-token` 으로 만든 CLAUDE_CODE_OAUTH_TOKEN 은 공식 문서가 CI 용이라 적어 두었고,
# 그걸로 돌리면 API 요금이 아니라 **구독 사용량**으로 처리된다(code.claude.com/docs/en/authentication).
# 붙여넣은 답과 똑같이 `apply` 를 지나므로 새로 생긴 뒷일은 없다. 한도·만료로 멈추면
# 그 자리부터 사람이 붙여넣게 이슈를 연다 — 아침 결과가 비는 날은 없다.

AUTO_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
AUTO_SYSTEM = ("당신은 부동산 뉴스 콘텐츠를 만드는 작가입니다. 도구는 쓰지 않습니다. "
               "사용자 메시지의 지시와 출력 규칙을 그대로 따르고, 요청한 JSON 을 ```json 상자 하나에 담아 답합니다.")
AUTO_FILES = {"brief": "1-brief.md", "blog": "2-blog.md", "video": "3-video.md"}


def auto_ready(cfg: Config) -> str:
    """자동으로 답할 수 있으면 빈 문자열, 아니면 못 하는 이유."""
    pc = cfg.get("paste", {}) or {}
    if not pc.get("auto", False):
        return "꺼져 있음"
    if not os.environ.get(AUTO_ENV):
        return f"구독 토큰({AUTO_ENV})이 없음"
    if not shutil.which(str(pc.get("claude_bin", "claude") or "claude")):
        return "claude 명령이 없음"
    return ""


def _step_model(cfg: Config, step: str) -> str:
    llm = cfg.get("llm", {}) or {}
    pick = {"brief": llm.get("brief_model"), "video": llm.get("script_model")}.get(step)
    return str(pick or llm.get("model") or "claude-sonnet-5")


def ask_claude(cfg: Config, step: str, prompt: str, *, model: str = "") -> tuple[str, str]:
    """Claude Code 에 한 번 묻는다. (답, 오류) — 둘 중 하나만 채워진다.

    · 도구를 모두 끈다(`--tools ""`) — 글만 쓰면 되는 일이라 파일·명령이 필요 없다.
    · 빈 임시 폴더에서 돈다 — 저장소에서 돌리면 긴 CLAUDE.md 를 읽어 답이 흐트러진다.
    · `--bare` 는 쓰지 않는다 — 문서상 그 모드는 CLAUDE_CODE_OAUTH_TOKEN 을 읽지 않는다.
    """
    import tempfile

    pc = cfg.get("paste", {}) or {}
    llm = cfg.get("llm", {}) or {}
    cap = int(llm.get("script_max_tokens" if step == "video" else "max_tokens", 16000) or 16000)
    cmd = [str(pc.get("claude_bin", "claude") or "claude"), "-p", "--output-format", "json",
           "--model", model or _step_model(cfg, step), "--tools", "",
           "--no-session-persistence", "--system-prompt", AUTO_SYSTEM]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(cap),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    env.pop("ANTHROPIC_API_KEY", None)        # 남아 있으면 구독 대신 API 로 청구된다 (인증 순서상 위)
    timeout = int(pc.get("auto_timeout_seconds", 900) or 900)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=tmp, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "", f"{timeout // 60}분 안에 답이 오지 않았습니다"
        except OSError as exc:
            return "", f"claude 를 실행하지 못했습니다: {exc}"
    try:
        data = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["(출력 없음)"]
        return "", f"claude 응답을 읽지 못했습니다: {tail[0][:200]}"
    if data.get("is_error") or done.returncode != 0:
        return "", str(data.get("result") or done.stderr or "알 수 없는 오류")[:300]
    usage = data.get("usage") or {}
    log.info("자동 답하기 %s: %s · 출력 %s토큰 · %.0f초", step, cmd[5],
             usage.get("output_tokens", "?"), (data.get("duration_ms") or 0) / 1000)
    return str(data.get("result") or ""), ""


def auto(cfg: Config, date_str: str, *, issues: list[Cluster] | None = None) -> Reply:
    """남은 단계를 차례로 Claude Code 에 묻고 반영한다. 한 단계라도 막히면 거기서 멈춘다.

    답이 JSON 검사에서 떨어지면 한 번 더 묻고, 모델 오류(한도·권한)면 대체 모델로 한 번 더
    묻는다. 그래도 안 되면 멈추고 사람 몫으로 넘긴다."""
    out_dir = cfg.output_dir / date_str
    total = Reply()
    fallback = str((cfg.get("llm", {}) or {}).get("fallback_model", "") or "")
    for step in STEPS:
        status = load_status(out_dir) or {}
        if (status.get("steps") or {}).get(step):
            continue
        path = paste_dir(out_dir) / AUTO_FILES[step]
        if not path.exists():
            total.problems.append(f"{STEP_TITLES[step]} 프롬프트가 없습니다 ({path.name})")
            break
        prompt = path.read_text(encoding="utf-8")
        applied = False
        for attempt in range(2):
            model = _step_model(cfg, step)
            text, err = ask_claude(cfg, step, prompt, model=model)
            if err and fallback and fallback != model:
                log.warning("자동 답하기 %s 실패(%s) — %s 로 한 번 더", step, err, fallback)
                text, err = ask_claude(cfg, step, prompt, model=fallback)
            if err:
                total.problems.append(f"{STEP_TITLES[step]}: {err}")
                break
            reply = apply(cfg, date_str, text, issues=issues)
            if step in reply.applied:
                total.applied.append(step)
                applied = True
                break
            why = "; ".join(reply.problems) or "반영되지 않았습니다"
            log.warning("자동 답하기 %s 반영 실패(%d번째): %s", step, attempt + 1, why)
            if attempt == 1:
                total.problems.append(f"{STEP_TITLES[step]}: {why}")
        if not applied:
            break
    total.done = bool((load_status(out_dir) or {}).get("done"))
    return total


# ── 아침 실행에서: 준비 ───────────────────────────────────────


def prepare(cfg: Config, renderer, issues: list[Cluster], date_str: str,
            stats_data: dict | None, result) -> str:
    """이슈(클러스터)·상태·1단계 프롬프트를 남기고 깃허브 이슈를 연다. 이슈 주소를 돌려준다.

    이슈 열기는 되면 좋고 안 되면 그만입니다 — gh 가 없거나 토큰이 없어도 아침 실행은
    성공해야 하고, 프롬프트는 prompt-pack.md 와 paste/1-brief.md 에 그대로 남습니다.
    """
    out_dir = renderer.out_dir
    pdir = paste_dir(out_dir)
    pdir.mkdir(parents=True, exist_ok=True)

    slim = []
    for c in issues:
        item = json.loads(c.model_dump_json())
        for a in item.get("articles", []):
            a["body"] = ""                    # 본문은 저작권이 있어 저장소에 올리지 않는다
        slim.append(item)
    (pdir / "issues.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1) + "\n",
                                       encoding="utf-8")

    prompt = brief_prompt(cfg, issues, date_str)
    (pdir / "1-brief.md").write_text(prompt, encoding="utf-8")
    renderer.prompt_pack(
        f"# 붙여넣기 프롬프트 — {date_str}\n\n{HOW_TO}\n\n{_fence(prompt)}\n\n"
        "2·3단계 프롬프트는 1단계 답을 되넣은 뒤에 만들어집니다 (이슈 댓글 또는 "
        f"`output/{date_str}/{DIR}/2-blog.md`, `3-video.md`).\n")

    status = load_status(out_dir) or {"date": date_str, "steps": {s: False for s in STEPS},
                                       "issue_url": "", "issue_number": 0}
    save_status(out_dir, status)
    for path in (pdir / "issues.json", pdir / "1-brief.md", pdir / "status.json"):
        if path not in renderer.written:
            renderer.written.append(path)

    # 구독으로 자동 답하기가 켜져 있으면 먼저 해 본다. 다 되면 사람 차례가 없다.
    body = issue_body(date_str, prompt, _repo_name(cfg))
    why = auto_ready(cfg)
    if not why:
        got = auto(cfg, date_str, issues=issues)
        result.paste_auto = list(got.applied)
        if got.done:
            result.llm_used = True
            log.info("자동 답하기로 세 단계를 모두 반영했습니다.")
            return ""
        status = load_status(out_dir) or status
        stop = "; ".join(got.problems) or "알 수 없는 이유"
        result.warnings.append(f"자동 답하기가 멈췄습니다 — {stop}. 남은 단계는 붙여넣기로 넘깁니다.")
        body = remaining_body(out_dir, date_str, _repo_name(cfg), status, why=stop)
        if status.get("done"):
            return ""
    elif why != "꺼져 있음":
        result.warnings.append(f"자동 답하기를 못 했습니다 ({why}) — 붙여넣기로 넘깁니다.")

    if not status.get("issue_url") and bool((cfg.get("paste", {}) or {}).get("open_issue", True)):
        url = open_issue(cfg, date_str, body)
        if url:
            status["issue_url"] = url
            m = re.search(r"/issues/(\d+)", url)
            status["issue_number"] = int(m.group(1)) if m else 0
    save_status(out_dir, status)

    if status.get("issue_url"):
        result.warnings.append(f"붙여넣기 차례 — 1단계 프롬프트: {status['issue_url']}")
    else:
        result.warnings.append(f"붙여넣기 차례 — 프롬프트: output/{date_str}/prompt-pack.md "
                               "(깃허브 이슈는 열지 못했습니다)")
    result.paste_url = status.get("issue_url", "")
    return result.paste_url


def _repo_name(cfg: Config) -> str:
    """owner/repo. 러너는 환경변수에 있고, 맥에서는 원격 주소에서 읽는다."""
    env = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env:
        return env
    try:
        url = subprocess.run(["git", "-C", str(cfg.repo_root), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    return m.group(1) if m else ""


def _gh(cfg: Config, *args: str, timeout: int = 60) -> str:
    """gh 명령 한 번. 실패는 예외 대신 빈 문자열."""
    gh = shutil.which("gh")
    if not gh:
        return ""
    repo = _repo_name(cfg)
    cmd = [gh, *args] + (["--repo", repo] if repo else [])
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gh 실패: %s", exc)
        return ""
    if done.returncode != 0:
        log.warning("gh 실패 (%s): %s", " ".join(args[:2]), done.stderr.strip()[:200])
        return ""
    return done.stdout.strip()


def open_issue(cfg: Config, date_str: str, body: str) -> str:
    """같은 날짜의 열린 이슈가 있으면 그것을, 없으면 새로 만들어 주소를 돌려준다."""
    title = f"{LABEL} · {date_str}"
    found = _gh(cfg, "issue", "list", "--label", LABEL, "--state", "open",
                "--search", f'"{title}" in:title', "--json", "number,url,title")
    try:
        for item in json.loads(found or "[]"):
            if item.get("title") == title:
                return str(item.get("url", ""))
    except json.JSONDecodeError:
        pass
    _gh(cfg, "label", "create", LABEL, "--color", "0E8A16", "--force",
        "--description", "claude.ai 에 붙여넣고 답을 댓글로 되넣는 날")
    tmp = Path(os.environ.get("RUNNER_TEMP") or "/tmp") / f"paste-issue-{date_str}.md"
    try:
        tmp.write_text(body, encoding="utf-8")
    except OSError:
        return ""
    return _gh(cfg, "issue", "create", "--title", title, "--label", LABEL, "--body-file", str(tmp))


# ── 댓글에서: 답 읽기 ─────────────────────────────────────────


def extract_json(text: str) -> tuple[list[dict], list[str]]:
    """댓글에서 JSON 덩이들을 꺼낸다. ```json 상자 → 통째 → 첫 { 부터 마지막 } 까지 순으로 시도."""
    found: list[dict] = []
    errors: list[str] = []
    blocks = re.findall(r"```(?:json|JSON)?[ \t]*\r?\n(.*?)```", text or "", re.S)
    for chunk in blocks or [text or ""]:
        chunk = chunk.strip()
        if not chunk:
            continue
        for candidate in (chunk, chunk[chunk.find("{"): chunk.rfind("}") + 1]):
            if not candidate or not candidate.startswith("{"):
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError as exc:
                errors.append(f"{exc.msg} (줄 {exc.lineno})")
                continue
            if isinstance(data, dict):
                found.append(data)
            break
    return found, errors


def classify(data: dict) -> str:
    if "issues" in data and "headline" in data:
        return "brief"
    if "body_markdown" in data:
        return "blog"
    if "shorts" in data and "longform" in data:
        return "video"
    return ""


@dataclass
class Reply:
    applied: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    done: bool = False
    text: str = ""


def apply(cfg: Config, date_str: str, text: str, *, issues: list[Cluster] | None = None) -> Reply:
    """댓글 하나를 반영한다. 여러 단계가 한 댓글에 있어도 순서대로 처리한다.

    `issues` 는 아침 실행 안에서 자동으로 답할 때 넘기는 **본문이 있는** 이슈다. 댓글로
    들어올 때는 저장소의 issues.json(본문을 비운 것)을 읽으므로 숫자 검산이 제목만 본다."""
    given_issues = issues
    from . import pipeline as pipe
    from .render import Renderer

    out_dir = cfg.output_dir / date_str
    reply = Reply()
    status = load_status(out_dir)
    if not status:
        reply.problems.append(f"{date_str} 은 붙여넣기 날이 아닙니다 (paste/status.json 없음).")
        reply.text = _reply_text(reply, status)
        return reply

    found, errors = extract_json(text)
    if not found:
        reply.problems.append("JSON 을 찾지 못했습니다" + (f": {errors[0]}" if errors else "")
                              + ". 답 전체를 ```json 상자에 담아 다시 붙여넣어 주세요.")
        reply.text = _reply_text(reply, status)
        return reply

    order = {s: i for i, s in enumerate(STEPS)}
    items = sorted(((classify(d), d) for d in found), key=lambda kd: order.get(kd[0], 9))
    repo = _repo_name(cfg)
    renderer = Renderer(cfg, out_dir, date_str)
    result = pipe.RunResult(date=date_str, out_dir=out_dir)
    stats_data = _load_json(out_dir / "stats.json") or None
    if (out_dir / "img-stats-volume.png").exists():
        renderer.stats_images = {"volume": "img-stats-volume.png"}
    made: dict = {}

    for step, data in items:
        if not step:
            reply.problems.append("어느 단계 답인지 알 수 없는 JSON 이 있습니다 (issues·body_markdown·shorts 가운데 하나가 있어야 합니다).")
            continue
        if step != "brief" and not status["steps"].get("brief"):
            reply.problems.append(f"{STEP_TITLES[step]} 답은 1단계가 먼저 반영돼야 넣을 수 있습니다.")
            continue
        try:
            if step == "brief":
                brief = DailyBrief.model_validate(data)
                issues = given_issues or load_issues(out_dir)
                pipe._after_brief(cfg, renderer, brief, issues, date_str, result, made)
                made["cards"] = renderer.cards(brief, stats=stats_data)
                (paste_dir(out_dir) / "brief.json").write_text(brief.model_dump_json(indent=1),
                                                                encoding="utf-8")
                blog_p, video_p = blog_prompt(cfg, brief), video_prompt(cfg, brief, stats_data)
                (paste_dir(out_dir) / "2-blog.md").write_text(blog_p, encoding="utf-8")
                (paste_dir(out_dir) / "3-video.md").write_text(video_p, encoding="utf-8")
                reply.text = next_comment(date_str, blog_p, video_p, repo,
                                          made=f"이슈 {len(brief.issues)}개 · 카드 {len(made['cards'])}장.")
            elif step == "blog":
                post = BlogPost.model_validate(data)
                brief, issues = _load_brief(out_dir), given_issues or load_issues(out_dir)
                made["brief"] = brief
                from .store import SeriesStore
                history = SeriesStore(cfg.state_dir / "datapoints.json").rows
                slot_files = renderer.images(brief, history=history, post=post)
                keys = pipe._after_blog(cfg, renderer, brief, post, issues, date_str, result, made,
                                        slot_files=slot_files, stats_data=stats_data, generator=None)
                (paste_dir(out_dir) / "keys.json").write_text(
                    json.dumps([k.__dict__ if hasattr(k, "__dict__") else k for k in keys],
                               ensure_ascii=False, default=str), encoding="utf-8")
                # 점검표는 호출마다 새로 쓴다. 3단계를 따로 넣는 날에도 블로그 항목이 남게
                # 글과 빈 사진 자리 수를 남겨 둔다 (_restore_made 가 읽는다).
                (paste_dir(out_dir) / "post.json").write_text(post.model_dump_json(indent=1),
                                                               encoding="utf-8")
                (paste_dir(out_dir) / "blog-extra.json").write_text(json.dumps(
                    {"empty_photo_slots": int(made.get("empty_photo_slots", 0) or 0)}),
                    encoding="utf-8")
            else:
                pack = VideoPack.model_validate(data)
                brief = _load_brief(out_dir)
                made["brief"] = brief
                keys = _load_keys(paste_dir(out_dir) / "keys.json")
                pipe._after_video(cfg, renderer, brief, pack, date_str, made, keys)
                (paste_dir(out_dir) / "pack.json").write_text(pack.model_dump_json(indent=1),
                                                               encoding="utf-8")
        except Exception as exc:                     # 검사 실패는 사람에게 돌려준다
            log.warning("%s 반영 실패: %s", step, exc, exc_info=True)
            reply.problems.append(f"{STEP_TITLES[step]}: {_short_error(exc)}")
            continue
        status["steps"][step] = True
        reply.applied.append(step)

    if reply.applied:
        result.llm_used = True
        _restore_made(cfg, out_dir, date_str, status, made, result, issues=given_issues)
        renderer.checklist(result, made, None)
        pipe._record_quality(cfg, date_str, renderer, made, result)
        save_status(out_dir, status)
        pipe.update_index(cfg)
    reply.done = bool(status.get("done"))
    if not reply.text or reply.problems or reply.done or "brief" not in reply.applied:
        reply.text = _reply_text(reply, status, site_url=str(cfg.get("site.url", "") or ""))
    return reply


def _restore_made(cfg, out_dir: Path, date_str: str, status: dict, made: dict, result,
                  *, issues: list[Cluster] | None = None) -> None:
    """앞선 댓글에서 반영한 단계의 결과를 되살린다 — 점검표·품질 장부가 그날 전체를 보게.

    점검표는 호출마다 새로 쓰는데 `made` 에는 이번 댓글에서 반영한 것만 있다. 3단계를
    따로 넣으면 블로그 점검 항목이 모두 빠지고 "막히는 것이 없습니다. 발행하세요" 가
    됐다 (2026-09-11 에 실제로 그랬다 — 사진 자리 ⚠️ 가 사라졌다)."""
    from . import pipeline as pipe
    from .store import previous_blog_bodies

    steps = status.get("steps") or {}
    pdir = paste_dir(out_dir)
    if steps.get("brief"):
        if made.get("brief") is None:
            made["brief"] = _load_brief(out_dir)
        brief = made.get("brief")
        if brief is not None and "checks" not in made:
            made["checks"] = pipe._verify_numbers(cfg, brief, issues or load_issues(out_dir), result)
        if brief is not None and "repeats" not in made:
            made["repeats"] = pipe._repeat_topics(cfg, brief, date_str)
    if steps.get("blog") and made.get("post") is None and (pdir / "post.json").exists():
        try:
            made["post"] = BlogPost.model_validate_json((pdir / "post.json").read_text(encoding="utf-8"))
        except Exception as exc:                     # 되살리지 못하면 그 항목만 빠진다
            log.warning("post.json 을 읽지 못했습니다: %s", exc)
        extra = _load_json(pdir / "blog-extra.json") or {}
        made.setdefault("empty_photo_slots", int(extra.get("empty_photo_slots", 0) or 0))
        made.setdefault("prev_bodies", previous_blog_bodies(
            cfg.output_dir, date_str, days=int(cfg.get("blog.overlap_lookback_days", 3))))
    if steps.get("video") and made.get("pack") is None and (pdir / "pack.json").exists():
        try:
            made["pack"] = VideoPack.model_validate_json((pdir / "pack.json").read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("pack.json 을 읽지 못했습니다: %s", exc)


def _load_keys(path: Path) -> list:
    """2단계에서 사전으로 저장한 핵심 수치를 KeyNumber 로 되살린다.

    사전 그대로 넘기면 썸네일이 `.display` 를 읽다 터진다 — 2026-09-11 아침 3단계가
    "'dict' object has no attribute 'display'" 로 실패했다. 모르는 칸은 버린다."""
    from .keynumbers import KeyNumber

    fields = ("display", "key", "label", "note")
    out = []
    for item in _load_json(path) or []:
        if isinstance(item, dict) and item.get("display"):
            out.append(KeyNumber(**{f: str(item.get(f, "") or "") for f in fields}))
    return out


def _reply_text(reply: Reply, status: dict | None, site_url: str = "") -> str:
    lines = []
    for s in reply.applied:
        lines.append(f"✅ {STEP_TITLES[s]} 반영했습니다.")
    for p in reply.problems:
        lines.append(f"❌ {p}")
    if status and not status.get("done"):
        left = [STEP_TITLES[s] for s in STEPS if not status["steps"].get(s)]
        if left:
            lines.append("남은 단계: " + " · ".join(left))
        if "brief" in reply.applied and reply.text:
            return reply.text + "\n" + "\n".join(lines)
    if status and status.get("done"):
        lines.append("🎉 세 단계가 다 됐습니다. 사이트가 곧 갱신됩니다"
                     + (f": {site_url.rstrip('/')}/latest/" if site_url else "."))
    return "\n".join(lines) or "반영할 것이 없었습니다."


def _load_brief(out_dir: Path) -> DailyBrief:
    return DailyBrief.model_validate_json((paste_dir(out_dir) / "brief.json").read_text(encoding="utf-8"))


def _load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _short_error(exc: Exception, limit: int = 600) -> str:
    text = str(exc)
    # pydantic 오류는 길다 — 필드 이름과 이유만 남긴다
    text = re.sub(r"\s+For further information visit \S+", "", text)
    text = re.sub(r"\[type=\S+, input_value=.*?\]", "", text, flags=re.S)
    return " ".join(text.split())[:limit]
