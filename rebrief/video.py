"""쇼츠 대본으로 자막이 얹힌 초안 영상(mp4)을 만든다.

완성본이 아니라 편집의 출발점이다. 자막 줄마다 한 장씩 카드(1080×1920)를 그려
타임코드대로 이어 붙인다. 소리는 없다. ffmpeg 와 헤드리스 크롬이 둘 다 있어야
하며, 없으면 아무것도 만들지 않고 조용히 넘어간다.
"""

from __future__ import annotations

import base64
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import images
from .models import ShortsScript
from .render import caption_timings

log = logging.getLogger(__name__)

W, H = 1080, 1920


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _data_uri(path: Path) -> str:
    mime = "image/png" if path.suffix == ".png" else "image/svg+xml"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def frame_svg(text: str, index: int, total: int, *, channel: str = "",
              image: Path | None = None, visual: str = "") -> str:
    """자막 카드 한 장. 위쪽에 그림(있으면), 아래쪽에 자막. 화면 지시는 작게 회색으로."""
    e = images.esc
    p = images.svg_open(W, H)
    # 상단: 채널명 + 진행 표시
    if channel:
        p.append(f'<text x="80" y="140" font-size="40" font-weight="700" fill="{images.BLUE}">{e(channel)}</text>')
    p.append(f'<text x="{W - 80}" y="140" font-size="36" text-anchor="end" fill="{images.MUTED}">{index}/{total}</text>')
    # 진행 막대
    p.append(f'<rect x="80" y="170" width="{W - 160}" height="6" rx="3" fill="{images.GRID}"/>')
    p.append(f'<rect x="80" y="170" width="{(W - 160) * index / max(total, 1):.0f}" height="6" rx="3" fill="{images.BLUE}"/>')

    # 중단: 그림 (가로에 맞춰 넣고 세로 비율은 그림이 정한다)
    if image is not None and image.exists():
        p.append(f'<image href="{_data_uri(image)}" x="80" y="260" width="{W - 160}" height="900" '
                 f'preserveAspectRatio="xMidYMid meet"/>')
    elif visual:
        # 그림이 없으면 화면 지시를 자리 표시로 보여 준다 (편집자가 채울 곳)
        p.append(f'<rect x="80" y="260" width="{W - 160}" height="900" rx="24" fill="#f0efec" '
                 f'stroke="{images.BASELINE}" stroke-width="3" stroke-dasharray="14 10"/>')
        for i, ln in enumerate(images.wrap(visual, 36, W - 320)[:5]):
            p.append(f'<text x="{W / 2}" y="{640 + i * 52}" font-size="36" text-anchor="middle" '
                     f'fill="{images.INK_2}">{e(ln)}</text>')

    # 하단: 자막 (최대 3줄, 크게)
    size = 78
    lines = images.wrap(text, size, W - 200)
    while len(lines) > 3 and size > 48:
        size -= 6
        lines = images.wrap(text, size, W - 200)
    lines = lines[:3]
    line_h = size * 1.3
    base = 1420
    # 상자는 글줄이 실제로 차지하는 높이(첫 줄 위 여백 + 줄 간격 × (n-1) + 마지막 줄 아래 여백)만큼만
    box_top = base - size * 0.95 - 34
    box_h = size * 0.95 + line_h * (len(lines) - 1) + size * 0.35 + 68
    p.append(f'<rect x="60" y="{box_top:.0f}" width="{W - 120}" height="{box_h:.0f}" rx="28" fill="{images.INK}" opacity="0.92"/>')
    for i, ln in enumerate(lines):
        p.append(f'<text x="{W / 2}" y="{base + i * line_h:.0f}" font-size="{size}" font-weight="800" '
                 f'text-anchor="middle" fill="#ffffff">{e(ln)}</text>')
    p.append("</svg>")
    return "\n".join(p)


def build_shorts_draft(out_dir: Path, shorts: ShortsScript, image_paths: list[Path], *,
                       channel: str = "", fps: int = 30,
                       filename: str = "shorts-draft.mp4") -> Path | None:
    """카드를 그려 이어 붙인다. 도구가 없거나 실패하면 None."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg or not images.find_browser():
        log.info("쇼츠 초안 건너뜀: ffmpeg 또는 크롬이 없습니다.")
        return None
    timings = caption_timings(shorts.lines, shorts.estimated_seconds)
    if not timings:
        return None

    pngs = [p for p in image_paths if p.suffix == ".png" and p.exists()]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        concat: list[str] = []
        for i, (line, (start, end)) in enumerate(zip(shorts.lines, timings), start=1):
            img = pngs[(i - 1) % len(pngs)] if pngs else None
            svg = tmp_dir / f"frame-{i:03d}.svg"
            svg.write_text(frame_svg(line.text, i, len(timings), channel=channel, image=img,
                                     visual=line.visual), encoding="utf-8")
            png = svg.with_suffix(".png")
            if not images.svg_to_png(svg, png, scale=1):
                log.warning("쇼츠 초안 건너뜀: 카드 %d 렌더 실패", i)
                return None
            concat.append(f"file '{png.name}'\nduration {max(end - start, 0.1):.3f}")
        # concat demuxer 는 마지막 파일을 한 번 더 적어야 duration 이 먹는다
        concat.append(f"file '{png.name}'")
        (tmp_dir / "list.txt").write_text("\n".join(concat) + "\n", encoding="utf-8")

        target = out_dir / filename
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               # -vsync 는 ffmpeg 7 에서 사라졌다. 출력 -r 만 주면 정지 이미지를 알아서 늘린다.
               "-i", str(tmp_dir / "list.txt"), "-r", str(fps),
               "-pix_fmt", "yuv420p", "-vf", f"scale={W}:{H}", str(target)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        except subprocess.CalledProcessError as exc:
            # ffmpeg 는 이유를 stderr 에만 쓴다. 삼키면 원인을 알 수 없다.
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            log.warning("쇼츠 초안 인코딩 실패 (exit %s): %s", exc.returncode, " | ".join(detail[-3:]) or "출력 없음")
            return None
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("쇼츠 초안 인코딩 실패: %s", exc)
            return None
    return target if target.exists() else None
