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
from .sanitize import clean_html

DATE_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")

# 사람이 매일 실제로 여는 문서들. (파일명, 화면에 보일 이름, 한 줄 설명)
PAGES = [
    ("blog-naver.html", "네이버 블로그 글", "버튼 눌러 복사하고 블로그에 붙여넣기"),
    ("brief.md", "오늘의 정리", "무슨 일이 있었는지 사실만 요약"),
    ("script-shorts.md", "쇼츠 대본", "60초. 자막과 화면 지시 포함"),
    ("script-longform.md", "롱폼 대본", "8분. 챕터와 자료화면 포함"),
    ("production-notes.md", "제작 메모", "제목·썸네일·태그·촬영 목록"),
    ("policy.md", "정부 발표 원문", "보도자료 3줄 요약과 원본 파일"),
    ("stats.md", "실거래가 통계", "정부 신고 자료를 직접 집계한 표"),
    ("sources.md", "기사 원문", "근거가 된 기사 링크"),
]
EXTRA_FILES = ["script-shorts.srt", "shorts-cuts.csv", "longform-chapters.csv", "data.json"]
# 그림·썸네일은 이름 패턴으로 통째로 복사한다.
ASSET_GLOBS = ["img-*.png", "img-*.svg", "thumb-*.png", "thumb-*.svg",
               "card-*.png", "card-*.svg"]      # 유튜브 게시물용 카드뉴스


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

    weeks = _build_periods(env, source / "weekly", dest / "weekly",
                           stem="weekly", title="주간 결산")
    months = _build_periods(env, source / "monthly", dest / "monthly",
                            stem="monthly", title="월간 결산")
    _build_dashboard(env, cfg, days, built, dest)
    _build_search(env, days, built, dest)
    upcoming = _build_upcoming(env, days, dest, cfg=cfg)

    from .store import PublishLog

    published = PublishLog(cfg.state_dir / "published.json")
    for entry in built:
        entry["published"] = published.get(entry["date"])

    today_entry = built[0] if built else {}
    index = env.get_template("site_index.html.j2").render(
        meta=meta_tags(
            site_base(cfg),
            title=str((cfg.get("video", {}) or {}).get("channel_name", "부돌보 브리핑")),
            description=(today_entry.get("description")
                         or "매일 아침 부동산 뉴스를 정리해 블로그 글과 영상 대본으로 만듭니다."),
            image=(f"{today_entry['date']}/{today_entry['image']}"
                   if today_entry.get("image") else ""),
            image_size=today_entry.get("size"),
            channel=str((cfg.get("video", {}) or {}).get("channel_name", "") or ""),
        ),
        has_feed=bool(site_base(cfg)),
        days=built,
        today=built[0] if built else None,
        weeks=weeks,
        months=months,
        has_dashboard=bool(days),
        has_search=any((d / "data.json").exists() for d in days),
        upcoming=upcoming,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        channel=(cfg.get("video", {}) or {}).get("channel_name", "부돌보 브리핑"),
    )
    (dest / "index.html").write_text(index, encoding="utf-8")

    _build_feed(cfg, built, dest)
    _build_sitemap(cfg, built, weeks + months, dest)

    _write_pwa(dest, channel=(cfg.get("video", {}) or {}).get("channel_name", "부돌보 브리핑"),
               png=bool((cfg.get("images", {}) or {}).get("png", True)))

    # Jekyll 이 밑줄로 시작하는 폴더를 무시하는 걸 막는다.
    (dest / ".nojekyll").write_text("", encoding="utf-8")
    return dest


def _write_pwa(dest: Path, channel: str, png: bool = True) -> None:
    """홈 화면에 앱처럼 추가되게 manifest 와 아이콘을 둔다. 서비스워커는 두지 않는다 —
    매일 바뀌는 페이지에 캐시가 남으면 어제 글을 보게 된다."""
    import json

    from . import images

    icon_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">'
        '<rect width="512" height="512" rx="96" fill="#256abf"/>'
        '<text x="256" y="318" font-size="220" font-weight="800" text-anchor="middle" fill="#fff" '
        'font-family="Apple SD Gothic Neo, Noto Sans KR, sans-serif">부</text></svg>'
    )
    (dest / "icon.svg").write_text(icon_svg, encoding="utf-8")
    icons = [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}]
    # iOS 는 PNG 만 받는다. 크롬이 있을 때만 만들고, 없으면 SVG 로만 둔다.
    if png and images.svg_to_png(dest / "icon.svg", dest / "icon-512.png", scale=1):
        icons.insert(0, {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"})
    (dest / "manifest.webmanifest").write_text(json.dumps({
        "name": channel, "short_name": channel[:8], "start_url": "./index.html",
        "display": "standalone", "background_color": "#f2f4f6", "theme_color": "#256abf",
        "lang": "ko", "icons": icons,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _stats_entries(day: Path) -> list[dict]:
    """그날 실거래 집계를 검색에 넣는다. 뉴스 수치만 색인하면 '노원구 전세가율' 로 찾아도 안 나온다.

    검색 화면이 이미 쓰는 모양(제목·한 줄·수치 목록)을 그대로 따르고, 링크만 통계 쪽으로 돌린다.
    """
    import json

    path = day / "stats.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    rows = data.get("districts") or []
    if not rows:
        return []

    numbers = [{"label": f"{r['name']} 거래", "value": r["now"]["count"], "unit": "건"} for r in rows]
    numbers += [{"label": f"{r['name']} 평균 거래가",
                 "value": round(r["now"]["avg"] / 100_000_000, 1), "unit": "억"} for r in rows]
    numbers += [{"label": f"{j['name']} 전세가율", "value": j["median"], "unit": "%"}
                for j in (data.get("jeonse") or [])]
    numbers += [{"label": f"{h['district']} {h['name']} {h['kind']}",
                 "value": round(h["amount"] / 100_000_000, 1), "unit": "억"}
                for h in (data.get("highlights") or [])]
    label = data.get("month_label", "")
    return [{
        "title": f"실거래 집계 — {label}",
        "category": "실거래",
        "one_liner": f"{label} 신고 매매 {data.get('total', 0):,}건 "
                     f"({data.get('before_label', '')} {data.get('total_before', 0):,}건). "
                     f"지역별 거래·평균가·전세가율·신고가.",
        "numbers": numbers,
        "href": "stats.html",
    }]


def _build_search(env, days: list[Path], built: list[dict], dest: Path) -> None:
    """모든 날의 data.json 을 색인 하나로 모아 브라우저에서만 찾는 검색 페이지."""
    import json

    first_page = {b["date"]: (b["pages"][0]["href"] if b["pages"] else "brief.html") for b in built}
    index = []
    for day in days:
        path = day / "data.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        issues = [{
            "title": i.get("title", ""), "category": i.get("category", ""),
            "one_liner": i.get("one_liner", ""),
            "numbers": [{"label": n.get("label", ""), "value": n.get("value", ""), "unit": n.get("unit", "")}
                        for n in i.get("numbers", [])],
        } for i in data.get("issues", [])]
        issues += _stats_entries(day)
        index.append({
            "date": day.name, "href": first_page.get(day.name, "brief.html"),
            "headline": data.get("headline", ""),
            "issues": issues,
        })
    if not index:
        return
    (dest / "search-index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    html = env.get_template("site_search.html.j2").render(
        days=len(index),
        # </script> 가 들어 있으면 페이지가 깨지므로 막아 둔다
        index_json=json.dumps(index, ensure_ascii=False).replace("</", "<\\/"),
    )
    (dest / "search.html").write_text(html, encoding="utf-8")


def _build_upcoming(env, days: list[Path], dest: Path, limit: int = 7,
                    cfg: Config | None = None) -> int:
    """브리핑마다 나온 '내일 볼 것' 과 정부 발표의 시행일을 한 장에 모은다."""
    import json

    from .cluster import similarity

    rows: list[dict] = []
    for day in days[:limit]:
        path = day / "data.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("tomorrow_watch", []) or []:
            text = " ".join(str(item).split())
            if not text or any(similarity(text, r["text"]) >= 0.7 for r in rows):
                continue
            rows.append({"text": text, "date": day.name})
    schedule = _policy_schedule(cfg, days)
    if not rows and not schedule:
        return 0
    lines = ["# 이번 주 볼 것", ""]
    if schedule:
        lines += ["## 정부 발표에 적힌 날짜", "",
                  "보도자료 원문에서 뽑은 일정입니다. 그날 무슨 일이 있는지 미리 적어 둡니다.", ""]
        lines += [f"- **{it['date']}** ({it['kind']}) — [{it['title']}]({it['url']})  "
                  f"\n  <small>{it['text']}</small>" for it in schedule]
        lines.append("")
    if rows:
        lines += ["## 브리핑에서 나온 확인거리", "",
                  "브리핑마다 나온 '내일 확인할 것' 을 모았습니다. 같은 말은 한 번만 실었습니다.", ""]
        lines += [f"- {r['text']}  \n  <small>{r['date']} 브리핑에서</small>" for r in rows]
    # 이 장만 날짜 칸이 아니라 **뿌리에** 놓입니다. 틀은 날짜 칸(site/<날짜>/) 을 전제로
    # `../` 를 쓰므로 그대로 두면 '← 목록' 이 부돌보 브리핑이 아니라 rokiz.net 최상위로
    # 나갑니다. 404 도 아니어서 눈에 안 띕니다 (2026-09-08 에 찾음).
    html = env.get_template("site_page.html.j2").render(
        title="이번 주 볼 것", date=days[0].name if days else "", base="",
        body_html=md_to_html("\n".join(lines)),
    )
    (dest / "upcoming.html").write_text(html, encoding="utf-8")
    return len(rows) + len(schedule)


def _policy_schedule(cfg: Config | None, days: list[Path]) -> list[dict]:
    """정부 발표에서 뽑아 둔 앞으로의 일정. 오늘 이후만."""
    if cfg is None:
        return []
    from .store import PolicyLog

    after = days[0].name if days else datetime.now().strftime("%Y-%m-%d")
    return PolicyLog(cfg.state_dir / "policies.json").upcoming(after)


def _stale_days(trades, today: str) -> int:
    """마지막으로 실거래를 받은 날로부터 며칠 지났는지. 자료가 아예 없으면 -1."""
    from datetime import date as _date

    if not trades.days or not today:
        return -1
    try:
        return (_date.fromisoformat(today) - _date.fromisoformat(max(trades.days))).days
    except ValueError:
        return -1


def _build_dashboard(env, cfg: Config, days: list[Path], built: list[dict], dest: Path,
                     limit: int = 30) -> None:
    """최근 N일의 수집·요약·그림·비용을 한 장에 모은다. 흩어진 로그를 보러 다니지 않게."""
    import json

    from .store import CostLog, QualityLog, TitleLog, TradeLog

    costs = CostLog(cfg.state_dir / "costs.json")
    by_date = costs.by_date()
    # 품질 장부는 산출물 폴더로 한 번 메운다 — 장부가 생기기 전 날이나 장부를 잃은 경우에도
    # 표가 비지 않게. 되살린 값은 저장하지 않는다 (그때그때 산출물에서 다시 읽으면 된다).
    book_quality = QualityLog(cfg.state_dir / "quality.json")
    book_quality.backfill(cfg.output_dir)
    krw = float(cfg.get("llm.krw_per_usd", 1400))
    first_page = {b["date"]: (b["pages"][0]["href"] if b["pages"] else "") for b in built}

    rows: list[dict] = []
    for day in days[:limit]:
        articles, feeds_ok, feeds_total = None, 0, 0
        # 건수·피드 상태는 collect.json(커밋됨)에서. 옛 날짜는 raw/articles.json 밖에 없을 수 있다.
        summary, raw = day / "collect.json", day / "raw" / "articles.json"
        try:
            if summary.exists():
                payload = json.loads(summary.read_text(encoding="utf-8"))
                articles = int(payload.get("articles", 0))
                feeds = payload.get("feeds", []) or []
            elif raw.exists():
                payload = json.loads(raw.read_text(encoding="utf-8"))
                articles = len(payload.get("articles", []))
                feeds = payload.get("meta", {}).get("feeds", []) or []
            else:
                feeds = []
            feeds_total = len(feeds)
            feeds_ok = sum(1 for f in feeds if f.get("ok"))
        except (json.JSONDecodeError, OSError, ValueError):
            pass
        llm = (day / "data.json").exists()
        note = ""
        if (day / "prompt-pack.md").exists() and not llm:
            note = "키 없음/실패 → 프롬프트 팩"
        rows.append({
            "date": day.name, "articles": articles, "feeds_ok": feeds_ok, "feeds_total": feeds_total,
            "llm": llm, "images": len(list(day.glob("img-*.png"))) + len(list(day.glob("thumb-*.png"))),
            "usd": by_date.get(day.name), "note": note, "first": first_page.get(day.name, ""),
        })

    counted = [r["articles"] for r in rows if r["articles"] is not None]
    feed_ok = sum(r["feeds_ok"] for r in rows)
    feed_total = sum(r["feeds_total"] for r in rows)
    week_usd, week_days = costs.recent(7)
    month_usd, month_days = costs.recent(30)
    cost_rows = [(d, u) for d, u in by_date.items()][-30:]
    cost_max = max((u for _, u in cost_rows), default=0.0) or 1.0
    cost_bars = [{"date": d, "usd": u, "h": round(100 * u / cost_max, 1)} for d, u in cost_rows]

    # 날마다 센 실거래 건수. 신고가 늦게 들어와 같은 달도 값이 커지므로 '쌓이는 속도' 로 읽는다.
    trades = TradeLog(cfg.state_dir / "trades.json")
    trade_rows = [(d, e) for d, e in sorted(trades.days.items())][-30:]
    trade_max = max((e.get("total", 0) for _, e in trade_rows), default=0) or 1
    trade_bars = [{"date": d, "total": e.get("total", 0), "month": e.get("month", ""),
                   "h": round(100 * e.get("total", 0) / trade_max, 1)} for d, e in trade_rows]
    trade_latest = dict(trade_rows[-1][1]) if trade_rows else {}
    if trade_latest.get("month"):
        from .store import _month_label

        trade_latest["month"] = _month_label(trade_latest["month"])   # '202607' 은 안 읽힌다

    from datetime import date as _date

    from .store import failure_streak

    html = env.get_template("site_dashboard.html.j2").render(
        rows=rows, krw=krw,
        fail_streak=failure_streak(cfg.output_dir, _date.fromisoformat(days[0].name)) if days else 0,
        week_usd=week_usd, week_days=week_days, month_usd=month_usd, month_days=month_days,
        avg_articles=round(sum(counted) / len(counted)) if counted else 0,
        feed_rate=round(100 * feed_ok / feed_total) if feed_total else 0,
        feed_ok=feed_ok, feed_total=feed_total,
        fail_days=sum(1 for r in rows if not r["llm"]),
        cost_bars=cost_bars, cost_max=cost_max,
        trade_bars=trade_bars, trade_latest=trade_latest,
        trade_stale=_stale_days(trades, days[0].name if days else ""),
        title_types=TitleLog(cfg.state_dir / "titles.json").by_type(),
        storage=_storage_use(cfg, len(days)),
        quality=book_quality.recent(30),
        quality_diff=book_quality.compare(),
    )
    (dest / "dashboard.html").write_text(html, encoding="utf-8")


def _age_days(day: str) -> int:
    """그 글이 며칠 전 것인지. 못 읽으면 0(배지를 달지 않는다)."""
    from datetime import date as _date

    try:
        return max((_date.today() - _date.fromisoformat(day)).days, 0)
    except ValueError:
        return 0


def _storage_use(cfg: Config, days: int) -> dict:
    """산출물 폴더가 얼마나 커졌는지. 지우지는 않고 **보이게만** 합니다.

    날마다 그림이 새로 커밋되고 깃 이력은 지워지지 않으므로, 그냥 두면 몇 해 뒤에 저장소가
    무거워집니다. 하루 평균과 '1년이면 얼마' 를 함께 내어 사람이 판단하게 합니다.
    """
    total = 0
    for path in cfg.output_dir.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    per_day = total / days if days else 0
    return {"mb": round(total / 1024 / 1024, 1),
            "per_day_mb": round(per_day / 1024 / 1024, 2),
            "year_gb": round(per_day * 365 / 1024 / 1024 / 1024, 2)}


def _build_periods(env, source: Path, dest: Path, *, stem: str, title: str) -> list[dict]:
    """output/<stem>/<이름>/ 을 사이트로 옮긴다. 최신이 앞.

    주간·월간이 폴더 이름과 파일 이름만 다르고 나머지가 같아 한 함수로 씁니다.
    """
    if not source.exists():
        return []
    periods: list[dict] = []
    for period_dir in sorted((p for p in source.iterdir() if p.is_dir()), reverse=True):
        md = period_dir / f"{stem}.md"
        naver = period_dir / f"{stem}-naver.html"
        if not md.exists() and not naver.exists():
            continue
        target = dest / period_dir.name
        target.mkdir(parents=True, exist_ok=True)
        # dir 는 사이트에서의 폴더 이름. 사이트맵이 주소를 만들 때 쓴다
        # (예전에는 이 값이 없어 weekly/ 가 빠진 주소가 사이트맵에 실렸다).
        entry = {"week": period_dir.name, "dir": dest.name, "pages": []}
        if naver.exists():
            shutil.copy2(naver, target / naver.name)
            entry["pages"].append({"href": naver.name, "label": "네이버 블로그 글"})
        for png in sorted(period_dir.glob("img-*.png")):
            shutil.copy2(png, target / png.name)
        if md.exists():
            html = env.get_template("site_page.html.j2").render(
                title=title, date=period_dir.name,
                body_html=md_to_html(md.read_text(encoding="utf-8")),
            )
            (target / f"{stem}.html").write_text(html, encoding="utf-8")
            entry["pages"].append({"href": f"{stem}.html", "label": "결산 읽기"})
        periods.append(entry)
    return periods


# 블로그에 올릴 때 실제로 쓰는 파일들 (문서가 아니라 '첨부물')
ZIP_GLOBS = ["img-*.png", "img-*.svg", "thumb-*.png", "thumb-*.svg",
             "script-shorts.srt", "shorts-cuts.csv", "longform-chapters.csv",
             "policy/*"]      # 정부 보도자료 원본(HWP·PDF)도 함께 묶는다

# 카드뉴스는 따로 묶습니다 — 가는 곳이 다릅니다.
# 블로그 첨부물은 네이버 글에 올리고, 카드뉴스는 유튜브 게시물에 올립니다.
# 한 봉투에 넣으면 유튜브에 올릴 때마다 필요 없는 자막·컷 리스트를 골라내야 합니다.
CARD_GLOBS = ["card-*.png", "card-*.svg"]


def _build_zip(dest: Path, name: str = "files.zip",
               globs: list[str] | None = None) -> dict | None:
    """그림·자막·컷 리스트를 한 파일로 묶는다. 브라우저에서 링크 한 번으로 받게.

    브라우저에서 자바스크립트로 묶지 않고 만들 때 미리 묶어 둔다 — 휴대폰에서도 확실히 받아진다.

    이름은 `2026-09-08_blogfiles.zip` 처럼 날짜를 앞에 답니다. 며칠치를 받아 두면
    내려받기 폴더에 `files.zip`, `files-1.zip` 이 쌓여 어느 날 것인지 알 수 없었습니다.
    """
    import zipfile

    files: list[Path] = []
    for pattern in (globs or ZIP_GLOBS):
        files += sorted(dest.glob(pattern))
    files = [f for f in files if f.name != name]
    files = [f for f in files if f.is_file()]
    if not files:
        return None
    target = dest / name
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    size = target.stat().st_size
    return {"href": name, "count": len(files),
            "size": f"{size / 1024 / 1024:.1f}MB" if size >= 1024 * 1024 else f"{size / 1024:.0f}KB"}


def _build_day(env, day: Path, dest: Path, cfg: Config) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    pages: list[dict] = []
    info = day_summary(day)          # 공유 카드에 쓸 제목·설명·이미지

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
                age_days=_age_days(day.name),
                body_html=md_to_html(source_file.read_text(encoding="utf-8")),
                meta=meta_tags(
                    site_base(cfg),
                    title=f"{info['headline'] or label} — {day.name}",
                    description=info["description"],
                    path=f"{day.name}/{href}",
                    image=f"{day.name}/{info['image']}" if info["image"] else "",
                    image_size=info["size"],
                    published=day.name,
                    channel=str((cfg.get("video", {}) or {}).get("channel_name", "") or ""),
                ),
            )
            (dest / href).write_text(html, encoding="utf-8")

        pages.append({"href": href, "label": label, "description": description})

    for filename in EXTRA_FILES:
        if (day / filename).exists():
            shutil.copy2(day / filename, dest / filename)

    if (day / "policy").is_dir():      # 정부 보도자료 원본 파일
        shutil.copytree(day / "policy", dest / "policy", dirs_exist_ok=True)

    assets = _copy_assets(day, dest)
    bundle = _build_zip(dest, f"{day.name}_blogfiles.zip")
    cards = _build_zip(dest, f"{day.name}_card_news.zip", CARD_GLOBS)
    if assets:
        # 그림 모아보기 페이지. 휴대폰에서 길게 눌러 저장하면 바로 블로그에 올릴 수 있다.
        html = env.get_template("site_images.html.j2").render(
            date=day.name, images=assets, bundle=bundle, cards=cards,
        )
        (dest / "images.html").write_text(html, encoding="utf-8")
        pages.append({
            "href": "images.html", "label": "그림·썸네일",
            "description": f"{len(assets)}장. 길게 눌러 저장 → 블로그에 올리기",
        })

    entry = {"date": day.name, "pages": pages, "checklist": None, "bundle": bundle,
             "cards": cards,
             "headline": info["headline"], "description": info["description"],
             "image": info["image"], "size": info["size"]}
    cl = day / "checklist.json"
    if cl.exists():
        try:
            import json
            data = json.loads(cl.read_text(encoding="utf-8"))
            entry["checklist"] = {"summary": data.get("summary", {}),
                                  "items": [i for i in data.get("items", []) if i.get("level") != "ok"][:6]}
            html = env.get_template("site_page.html.j2").render(
                title="발행 전 점검", date=day.name,
                body_html=md_to_html((day / "checklist.md").read_text(encoding="utf-8")) if (day / "checklist.md").exists() else "",
            )
            (dest / "checklist.html").write_text(html, encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass
    return entry


def _copy_assets(day: Path, dest: Path) -> list[dict]:
    """img-*/thumb-* 파일을 복사하고 PNG 목록을 돌려준다 (SVG 는 복사만)."""
    found: list[dict] = []
    for pattern in ASSET_GLOBS:
        for path in sorted(day.glob(pattern)):
            shutil.copy2(path, dest / path.name)
            if path.suffix == ".png":
                found.append({"file": path.name, "label": _asset_label(path.name)})
    return found


# 카드 파일 이름(card-<쪽>-<낯>.png)에서 낯을 사람 말로 옮긴다.
_CARD_FACES = {"cover": "표지", "numbers": "오늘의 숫자", "issue": "이슈",
               "watch": "내일 볼 것", "rest": "나머지 이슈"}
_CARD_NAME = re.compile(r"^card-(\d+)-([a-z]+)$")

_ASSET_LABELS = {
    "0-cover": "대표 이미지 (글 맨 위)",
    "district-map": "서울 자치구 도식",
    # 실거래 통계 그림들. 없으면 파일명이 그대로 화면과 대체 텍스트에 나갔습니다.
    "stats-map-jeonse": "서울 자치구별 전세가율 지도",
    "stats-map": "서울 자치구별 아파트 매매 거래 지도",
    "stats-index": "부동산원 지수 비교 그래프",
    "stats-supply": "주택 인허가 추이 그래프",
    "stats-volume": "자치구별 거래량 막대그래프",
    "index-comparison": "지수 비교",
    "stat-card": "수치 카드",
    "time-series": "추이 그래프",
    "thumb-longform-2": "롱폼 썸네일 2안",
    "thumb-shorts-2": "쇼츠 썸네일 2안",
    "thumb-longform": "롱폼 썸네일 1안",
    "thumb-shorts": "쇼츠 썸네일 1안",
}


def _asset_label(filename: str) -> str:
    """그림 하나의 이름표. 화면에도 쓰고 **대체 텍스트(alt)** 로도 나간다.

    카드는 전부 「카드뉴스」로만 읽히던 때가 있었습니다 — 화면 낭독기로 들으면 여섯 장이
    똑같은 말이라 어느 장인지 알 수 없습니다 (2026-09-08 에 고침).
    """
    stem = filename.rsplit(".", 1)[0]
    card = _CARD_NAME.match(stem)
    if card:
        face = _CARD_FACES.get(card.group(2), card.group(2))
        return f"카드뉴스 {int(card.group(1))} {face}"
    for key, label in _ASSET_LABELS.items():
        if key in stem:
            return label
    return stem


_CHECKED = re.compile(r"<li>\[([ xX])\]\s*")


def md_to_html(text: str) -> str:
    """마크다운을 HTML 로. 체크박스 목록은 눈에 보이는 기호로 바꾼다."""
    html = clean_html(markdown_lib.markdown(
        text or "", extensions=["tables", "sane_lists"], output_format="html"
    ))
    return _CHECKED.sub(lambda m: "<li>☑ " if m.group(1).lower() == "x" else "<li>☐ ", html)


# ── 공유 카드·검색엔진·구독 피드 ───────────────────────────────
#
# 카카오톡·트위터에 주소를 붙이면 미리보기 카드가 뜨고(og:*), 검색엔진이 글의
# 제목·날짜를 구조로 읽으며(JSON-LD), 구독기가 새 글을 받아 갑니다(feed.xml).
# 전부 정적 파일이라 서버는 필요 없습니다.


def site_base(cfg: Config) -> str:
    """설정된 사이트 주소를 항상 슬래시로 끝나게 다듬는다. 없으면 빈 문자열."""
    url = str(cfg.get("site.url", "") or "").strip()
    return (url.rstrip("/") + "/") if url else ""


def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def meta_tags(base: str, *, title: str, description: str = "", path: str = "",
              image: str = "", image_size: tuple[int, int] | None = None,
              published: str = "", channel: str = "") -> str:
    """공유 카드용 태그 묶음. base 가 비어 있으면 상대 주소라도 넣어 둔다."""
    url = base + path.lstrip("/")
    tags = [f'<meta name="description" content="{_esc_attr(description)}">'] if description else []
    if url:
        tags.append(f'<link rel="canonical" href="{_esc_attr(url)}">')
    tags += [
        '<meta property="og:type" content="' + ("article" if published else "website") + '">',
        f'<meta property="og:title" content="{_esc_attr(title)}">',
        f'<meta property="og:site_name" content="{_esc_attr(channel or title)}">',
        '<meta property="og:locale" content="ko_KR">',
    ]
    if description:
        tags.append(f'<meta property="og:description" content="{_esc_attr(description)}">')
    if url:
        tags.append(f'<meta property="og:url" content="{_esc_attr(url)}">')
    if image:
        # 카드 이미지는 상대 주소를 받아 주지 않는 곳이 많아 전체 주소로만 넣는다
        full = image if image.startswith("http") else (base + image.lstrip("/") if base else "")
        if full:
            tags.append(f'<meta property="og:image" content="{_esc_attr(full)}">')
            if image_size:                     # 실제 크기와 다르게 적으면 카드가 잘린다
                tags += [f'<meta property="og:image:width" content="{image_size[0]}">',
                         f'<meta property="og:image:height" content="{image_size[1]}">']
            tags += ['<meta name="twitter:card" content="summary_large_image">',
                     f'<meta name="twitter:image" content="{_esc_attr(full)}">']
        else:
            tags.append('<meta name="twitter:card" content="summary">')
    else:
        tags.append('<meta name="twitter:card" content="summary">')
    tags.append(f'<meta name="twitter:title" content="{_esc_attr(title)}">')
    if description:
        tags.append(f'<meta name="twitter:description" content="{_esc_attr(description)}">')
    if published:
        payload = {
            "@context": "https://schema.org", "@type": "NewsArticle",
            "headline": title[:110], "datePublished": published, "dateModified": published,
            "inLanguage": "ko-KR",
        }
        if description:
            payload["description"] = description
        if url:
            payload["url"] = url
            payload["mainEntityOfPage"] = url
        if image and base:
            payload["image"] = [base + image.lstrip("/")]
        if channel:
            payload["publisher"] = {"@type": "Organization", "name": channel}
        import json as _json

        body = _json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        tags.append(f'<script type="application/ld+json">{body}</script>')
    return "\n".join(tags)


def _esc_attr(text: str) -> str:
    """따옴표 속에 들어갈 문자열. 줄바꿈은 공백으로 눕힌다."""
    return _xml_escape(" ".join(str(text).split()))


def day_summary(day: Path) -> dict:
    """그날 data.json 에서 카드에 쓸 제목·설명·이미지를 뽑는다."""
    import json

    info = {"headline": "", "description": "", "image": "", "size": None}
    path = day / "data.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        info["headline"] = str(data.get("headline", "") or "")
        lines = [str(i.get("one_liner", "") or "") for i in data.get("issues", [])]
        info["description"] = _trim(" · ".join(x for x in lines if x), 150)
    # 카드 이미지는 가로로 넓은 것부터. 크기를 함께 적어야 카드가 잘리지 않는다.
    for name, size in (("img-0-cover.png", (1200, 630)), ("thumb-longform.png", (1280, 720))):
        if (day / name).exists():
            info["image"], info["size"] = name, size
            break
    return info


def _trim(text: str, limit: int) -> str:
    """길면 자르되 낱말 중간에서 끊지 않는다."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    mark = max(cut.rfind(" · "), cut.rfind(". "), cut.rfind(" "))
    return (cut[:mark] if mark > limit * 0.5 else cut).rstrip(" ·.,") + "…"


_WDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _rfc822(date_str: str) -> str:
    """구독기가 읽는 날짜 형식. 요일·달 이름을 직접 적는다 —
    strftime 은 컴퓨터의 언어 설정에 따라 한글을 내보낼 수 있다."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return ""
    return (f"{_WDAY[d.weekday()]}, {d.day:02d} {_MONTH[d.month - 1]} {d.year} "
            "07:00:00 +0900")          # 브리핑이 나오는 한국 시간 오전 7시


def _build_feed(cfg: Config, built: list[dict], dest: Path, limit: int = 20) -> None:
    """구독기(RSS)용 feed.xml. 주소가 설정돼 있지 않으면 만들지 않는다 — 상대 주소는 구독기가 못 읽는다."""
    base = site_base(cfg)
    if not base or not built:
        return
    channel = (cfg.get("video", {}) or {}).get("channel_name", "부돌보 브리핑")
    items = []
    for entry in built[:limit]:
        date_str = entry["date"]
        hrefs = [p["href"] for p in entry["pages"]]
        # 구독자가 읽을 페이지를 건다. 붙여넣기용 화면은 버튼만 잔뜩이라 뒤로 미룬다.
        href = next((h for h in ("brief.html", "blog.html") if h in hrefs),
                    hrefs[0] if hrefs else "brief.html")
        link = f"{base}{date_str}/{href}"
        title = entry.get("headline") or f"{date_str} 부돌보 브리핑"
        stamp = _rfc822(date_str)
        if not stamp:
            continue
        items.append(
            "<item>"
            f"<title>{_xml_escape(title)}</title>"
            f"<link>{_xml_escape(link)}</link>"
            f"<guid isPermaLink=\"true\">{_xml_escape(link)}</guid>"
            f"<pubDate>{stamp}</pubDate>"
            f"<description>{_xml_escape(entry.get('description') or title)}</description>"
            "</item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>{_xml_escape(channel)}</title>"
        f"<link>{_xml_escape(base)}</link>"
        "<description>매일 아침 부동산 뉴스 브리핑</description>"
        "<language>ko</language>"
        + "".join(items) +
        "</channel></rss>\n"
    )
    (dest / "feed.xml").write_text(xml, encoding="utf-8")


def _build_sitemap(cfg: Config, built: list[dict], weeks: list[dict], dest: Path) -> None:
    """검색엔진이 훑을 주소 목록. 새 글을 더 빨리 찾아갑니다."""
    base = site_base(cfg)
    if not base:
        return
    urls = [(base, "")]
    for entry in built:
        for page in entry["pages"]:
            urls.append((f"{base}{entry['date']}/{page['href']}", entry["date"]))
    for period in weeks:
        folder = period.get("dir", "")
        prefix = f"{folder}/" if folder else ""
        for page in period["pages"]:
            urls.append((f"{base}{prefix}{period['week']}/{page['href']}", ""))
    body = "".join(
        "<url><loc>" + _xml_escape(u) + "</loc>"
        + (f"<lastmod>{d}</lastmod>" if d else "")
        + "</url>"
        for u, d in urls
    )
    (dest / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>\n",
        encoding="utf-8")
    (dest / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base}sitemap.xml\n", encoding="utf-8")
