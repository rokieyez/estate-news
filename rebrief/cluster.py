"""같은 사건을 다룬 기사들을 하나의 이슈로 묶는다.

한국어 형태소 분석기(KoNLPy 등)는 Java 의존성이 있어 CI에서 다루기 번거롭다.
제목의 **글자 2-gram Dice 계수**만 써도 뉴스 중복 판정은 충분히 되므로
외부 의존성 없이 이 방식을 쓴다.

Jaccard 대신 Dice 를 쓰는 이유: 같은 사건이라도 매체마다 제목 길이가 크게
달라서("서울 아파트값 3주 연속 하락…낙폭은 축소" vs "서울 아파트값 3주 연속
내림세"), 합집합으로 나누는 Jaccard 는 긴 쪽이 손해를 본다. Dice 는 두 집합
크기의 합으로 나눠 길이 차이에 덜 민감하다.

실측 (한국 부동산 헤드라인 표본):
    같은 사건    0.35 ~ 0.69
    다른 사건    0.00 ~ 0.40   (같은 지역·자산을 다룬 다른 기사가 상단)
두 구간이 조금 겹치므로 완벽한 분리는 안 된다. 기본값 0.35 는 **재현율 쪽**을
택한 값이다. 같은 사건이 두 이슈로 쪼개지면 브리핑에 중복 꼭지가 생겨 눈에
띄게 나쁘지만, 인접한 두 기사가 한 이슈로 묶이는 건 요약에서 자연스럽게
흡수되기 때문이다. 정밀도를 원하면 settings.yaml 에서 0.45 이상으로 올리면 된다.
"""

from __future__ import annotations

import re

from .config import Config
from .models import Article, Cluster

_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]")


def normalize(title: str) -> str:
    return _NON_WORD.sub("", title)


def bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """제목 두 개의 글자 2-gram Dice 계수 (0~1)."""
    ga, gb = bigrams(normalize(a)), bigrams(normalize(b))
    if not ga or not gb:
        return 0.0
    intersection = len(ga & gb)
    if not intersection:
        return 0.0
    return 2 * intersection / (len(ga) + len(gb))


def build_clusters(cfg: Config, articles: list[Article]) -> list[Cluster]:
    """기사 목록을 이슈 단위로 묶는다.

    각 기사는 기존 클러스터의 '대표 기사'와만 비교한다. 모든 구성원과 비교하면
    A-B, B-C 는 비슷한데 A-C 는 전혀 다른 기사들이 사슬처럼 엮이는 문제가 생긴다.
    """
    threshold = float(cfg.get("cluster.similarity_threshold", 0.40))
    clusters: list[Cluster] = []

    for article in articles:
        best: Cluster | None = None
        best_score = threshold

        for cluster in clusters:
            score = similarity(article.title, cluster.articles[0].title)
            if score >= best_score:
                best, best_score = cluster, score

        if best is None:
            clusters.append(Cluster(key=article.id, articles=[article]))
        else:
            best.articles.append(article)

    return clusters
