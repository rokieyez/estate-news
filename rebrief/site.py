"""output/ 폴더를 휴대폰에서 볼 수 있는 정적 사이트로 만든다.

깃허브에서는 HTML 파일이 소스 코드로만 보이고, 마크다운은 휴대폰에서 읽기 불편하다.
GitHub Pages 로 올릴 수 있는 site/ 를 만들어 두면 링크 하나만 즐겨찾기 해두고
매일 아침 그것만 열면 된다.

  site/index.html          오늘 할 일 + 지난 날짜 목록
  site/latest/...          항상 가장 최근 날짜 (주소가 안 바뀜)
  site/2026-09-06/...      날짜별 보관
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

import markdown as markdown_lib

from .config import Config
from .render import make_env

DATE_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")

# 사람이 매일 실제로 여는 문서들. (파일명, 화면에 보일 이름, 한 줄 설명)
PAGES = [
    ("blog-naver.html", "네이버 블로그 글", "버튼 눌러 복사하고 블로그에 붙여넣기"),
    ("brief.md", "오늘의 정리", "무슨 일이 있었는지 사실만 요약"),
    ("script-shorts.md", "쇼츠 대본", "60초. 자막과 화면 지시 포함"),
    ("script-longform.md", "롱폼 대본", "8분. 챕터와 자료화면 포함"),
    ("production-notes.md", "제작 메모", "제목·썸네일·태그·촬영 목록"),
    ("sources.md", "기사 원문", "근거가 된 기사 링크"),
]
EXTRA_FILES = ["script-shorts.srt", "data.json"]
# 그림·썸네일은 이름 패턴으로 통째로 복사한다. 영상 초안(mp4)은 용량 때문에 뺀다.
ASSET_GLOBS = ["img-*.png", "img-*.svg", "thumb-*.png", "thumb-*.svg"]


def build_site(cfg: Config, dest: Path | None = None) -> Path:
    """정적 사이트를 만들고 그 폴더 경로를 돌려준다."""
    source = cfg.output_dir
    dest = dest or (cfg.repo_root / "site")
    env = make_env()

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    days = sorted(
        (p for p in source.iterdir() if p.is_dir() and DATE_DIR.fullmatch(p.name)),
        reverse=True,
    ) if source.exists() else []

    built: list[dict] = []
    for day in days:
        entry = _build_day(env, day, dest / day.name, cfg)
        if entry["pages"]:
            built.append(entry)

    # 가장 최근 날짜를 latest/ 로 한 번 더 복사한다.
    # 심볼릭 링크는 GitHub Pages 에서 깨지므로 실제 복사본을 둔다.
    if built:
        newest = dest / built[0]["date"]
        shutil.copytree(newest, dest / "latest")

    weeks = _build_weeks(env, source / "weekly", dest / "weekly")

    index = env.get_template("site_index.html.j2").render(
        days=built,
        today=built[0] if built else None,
        weeks=weeks,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        channel=(cfg.get("video", {}) or {}).get("channel_name", "부동산 브리핑"),
    )
    (dest / "index.html").write_text(index, encoding="utf-8")

    # Jekyll 이 밑줄로 시작하는 폴더를 무시하는 걸 막는다.
    (dest / ".nojekyll").write_text("", encoding="utf-8")
    return dest


def _build_weeks(env, source: Path, dest: Path) -> list[dict]:
    """output/weekly/<주차>/ 를 사이트로 옮긴다. 최신 주가 앞."""
    if not source.exists():
        return []
    weeks: list[dict] = []
    for week_dir in sorted((p for p in source.iterdir() if p.is_dir()), reverse=True):
        md = week_dir / "weekly.md"
        naver = week_dir / "weekly-naver.html"
        if not md.exists() and not naver.exists():
            continue
        target = dest / week_dir.name
        target.mkdir(parents=True, exist_ok=True)
        entry = {"week": week_dir.name, "pages": []}
        if naver.exists():
            shutil.copy2(naver, target / naver.name)
            entry["pages"].append({"href": naver.name, "label": "네이버 블로그 글"})
        if md.exists():
            html = env.get_template("site_page.html.j2").render(
                title="주간 결산", date=week_dir.name,
                body_html=md_to_html(md.read_text(encoding="utf-8")),
            )
            (target / "weekly.html").write_text(html, encoding="utf-8")
            entry["pages"].append({"href": "weekly.html", "label": "결산 읽기"})
        weeks.append(entry)
    return weeks


def _build_day(env, day: Path, dest: Path, cfg: Config) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    pages: list[dict] = []

    for filename, label, description in PAGES:
        source_file = day / filename
        if not source_file.exists():
            continue

        if filename.endswith(".html"):
            shutil.copy2(source_file, dest / filename)
            href = filename
        else:
            href = filename.replace(".md", ".html")
            html = env.get_template("site_page.html.j2").render(
                title=label,
                date=day.name,
                body_html=md_to_html(source_file.read_text(encoding="utf-8")),
            )
            (dest / href).write_text(html, encoding="utf-8")

        pages.append({"href": href, "label": label, "description": description})

    for filename in EXTRA_FILES:
        if (day / filename).exists():
            shutil.copy2(day / filename, dest / filename)

    assets = _copy_assets(day, dest)
    if assets:
        # 그림 모아보기 페이지. 휴대폰에서 길게 눌러 저장하면 바로 블로그에 올릴 수 있다.
        html = env.get_template("site_images.html.j2").render(
            date=day.name, images=assets,
        )
        (dest / "images.html").write_text(html, encoding="utf-8")
        pages.append({
            "href": "images.html", "label": "그림·썸네일",
            "description": f"{len(assets)}장. 길게 눌러 저장 → 블로그에 올리기",
        })

    return {"date": day.name, "pages": pages}


def _copy_assets(day: Path, dest: Path) -> list[dict]:
    """img-*/thumb-* 파일을 복사하고 PNG 목록을 돌려준다 (SVG 는 복사만)."""
    found: list[dict] = []
    for pattern in ASSET_GLOBS:
        for path in sorted(day.glob(pattern)):
            shutil.copy2(path, dest / path.name)
            if path.suffix == ".png":
                found.append({"file": path.name, "label": _asset_label(path.name)})
    return found


_ASSET_LABELS = {
    "district-map": "서울 자치구 도식",
    "index-comparison": "지수 비교",
    "stat-card": "수치 카드",
    "time-series": "추이 그래프",
    "thumb-longform": "롱폼 썸네일",
    "thumb-shorts": "쇼츠 썸네일",
}


def _asset_label(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    for key, label in _ASSET_LABELS.items():
        if key in stem:
            return label
    return stem


_CHECKED = re.compile(r"<li>\[([ xX])\]\s*")


def md_to_html(text: str) -> str:
    """마크다운을 HTML 로. 체크박스 목록은 눈에 보이는 기호로 바꾼다."""
    html = markdown_lib.markdown(
        text or "", extensions=["tables", "sane_lists"], output_format="html"
    )
    return _CHECKED.sub(lambda m: "<li>☑ " if m.group(1).lower() == "x" else "<li>☐ ", html)
