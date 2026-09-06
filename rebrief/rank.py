"""이슈 클러스터에 점수를 매겨 오늘 다룰 것을 고른다.

점수 = 보도량 + 키워드 중요도 + 최신성 + 매체 가중치 + 속보 가점
가중치는 config/settings.yaml 의 rank 항목에서 조절한다.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .config import Config
from .models import Cluster


def score_clusters(cfg: Config, clusters: list[Cluster], now: datetime | None = None) -> list[Cluster]:
    """모든 클러스터에 점수·카테고리를 채우고 높은 순으로 정렬해 돌려준다."""
    now = now or datetime.now(timezone.utc)
    lookback = max(int(cfg.get("run.lookback_hours", 28)), 1)
    weights = cfg.get("rank", {}) or {}
    # 21개 매체가 받아쓴 보도자료가 6개 매체가 다룬 실제 이슈를 이기면 안 된다.
    # 어느 지점을 넘으면 매체가 더 늘어도 새로운 정보는 없다고 보고 잘라낸다.
    volume_cap = int(cfg.get("rank.volume_cap", 8))
    keywords = cfg.keywords
    breaking_tags = set(cfg.breaking_tags)

    for cluster in clusters:
        categories, matched, keyword_score = _match_keywords(cluster, keywords)
        cluster.categories = categories
        cluster.matched_keywords = matched

        volume = math.log(1 + min(cluster.size, volume_cap))
        recency = _recency(cluster, now, lookback)
        source = sum(a.source_weight for a in cluster.articles) / cluster.size
        breaking = 1.0 if any(set(a.tags) & breaking_tags for a in cluster.articles) else 0.0

        cluster.score = (
            float(weights.get("volume", 3.0)) * volume
            + float(weights.get("keyword", 1.0)) * keyword_score
            + float(weights.get("recency", 1.5)) * recency
            + float(weights.get("source", 0.8)) * source
            + float(weights.get("breaking", 1.2)) * breaking
        )

    clusters.sort(key=lambda c: c.score, reverse=True)
    return clusters


def select_issues(cfg: Config, clusters: list[Cluster]) -> list[Cluster]:
    """점수순 상위 이슈만 남긴다. 키워드가 하나도 안 걸린 클러스터는 제외."""
    min_size = int(cfg.get("run.min_cluster_size", 1))
    max_issues = int(cfg.get("run.max_issues", 5))

    candidates = [
        c for c in clusters
        if c.size >= min_size and c.categories
    ]
    # 키워드가 전혀 안 걸렸더라도 여러 매체가 동시에 다뤘다면 살려 둔다.
    if len(candidates) < max_issues:
        for cluster in clusters:
            if cluster in candidates:
                continue
            if cluster.size >= max(min_size, 2):
                candidates.append(cluster)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_issues]


def _match_keywords(cluster: Cluster, keywords: dict) -> tuple[list[str], list[str], float]:
    """클러스터 전체 텍스트에서 키워드 사전을 매칭한다."""
    haystack = " ".join(a.text_for_matching for a in cluster.articles)

    categories: list[str] = []
    matched: list[str] = []
    total = 0.0

    for category, spec in keywords.items():
        terms = spec.get("terms", []) or []
        hits = [t for t in terms if t in haystack]
        if not hits:
            continue
        categories.append(category)
        matched.extend(hits)
        # 같은 카테고리 안에서 여러 단어가 걸려도 가중치가 폭주하지 않게 감쇠시킨다.
        total += float(spec.get("weight", 1.0)) * min(1.0 + 0.15 * (len(hits) - 1), 2.0)

    return categories, matched, total


def _recency(cluster: Cluster, now: datetime, lookback_hours: int) -> float:
    """가장 최근 기사 기준 0~1. 방금 나온 기사면 1에 가깝다."""
    times = [a.published for a in cluster.articles if a.published]
    if not times:
        return 0.5
    newest = max(times)
    hours = (now - newest).total_seconds() / 3600
    return max(0.0, min(1.0, 1.0 - hours / lookback_hours))
