#!/usr/bin/env python3
"""
所属 Skill：Skill 2 — 学术人物定向检索与人才评估 Skill
前置依赖：Python 3.11+；在线运行需组织批准的公开 API/搜索 API 配置。
Agent 调用入口：
    python academic_search_analysis_script.py \
      --input search-input.json --config providers.json --output profile.json

边界：一次调用只对 Agent 给定的「姓名+机构+研究方向」做固定查询扇出和
确定性分析。脚本不会改写关键词、不会决定是否补搜/触达，也不生成文案。
HTTP 重试只处理同一请求的 429/5xx，不构成语义多轮检索。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = "1.0"
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
PRIVATE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com",
    "qq.com", "163.com", "126.com", "sina.com", "foxmail.com",
}
DEPARTMENTAL_LOCALS = {
    "info", "contact", "office", "admin", "administrator", "department",
    "dept", "secretary", "recruitment", "hr", "career", "zhaosheng", "xgb",
}
TRANSLATION_TERMS = {
    "industry", "industrial", "commercialization", "commercialisation",
    "translation", "translational", "patent", "startup", "spinout",
    "clinical", "deployment", "product", "产业", "转化", "专利", "临床", "产品",
}
LEADERSHIP_TERMS = {
    "professor", "principal investigator", "director", "group leader", "chair",
    "head of", "laboratory head", "chief scientist", "教授", "研究员", "主任",
    "课题组长", "首席科学家",
}
STOPWORDS = {
    "and", "the", "for", "with", "from", "into", "using", "that", "this",
    "role", "work", "team", "research", "candidate", "preferred", "required",
    "must", "have", "will", "our", "your", "you", "of", "in", "on", "to",
    "a", "an", "or", "及", "和", "与", "的", "方向", "岗位", "要求",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def unique_strings(values: Iterable[Any] | Any | None) -> list[str]:
    if values is None:
        values = []
    elif isinstance(values, (str, bytes)):
        values = [values]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = compact(value).strip(" ,;")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_iso_date(value: Any) -> date | None:
    text = compact(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def text_tokens(value: Any) -> set[str]:
    text = compact(value).casefold()
    latin = re.findall(r"[a-z][a-z0-9+\-]{2,}", text)
    chinese_chunks = re.findall(r"[\u3400-\u9fff]{2,8}", text)
    chinese: list[str] = []
    for chunk in chinese_chunks:
        chinese.append(chunk)
        if len(chunk) > 2:
            chinese.extend(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return {token for token in latin + chinese if token not in STOPWORDS}


def overlap_ratio(needles: Iterable[Any], corpus: str) -> float:
    required = text_tokens(" ".join(compact(item) for item in needles))
    if not required:
        return 0.0
    present = text_tokens(corpus)
    return len(required & present) / len(required)


def dimension_coverage(needles: Iterable[Any], corpus: str) -> float:
    """Average requirement-level coverage without penalizing long JD lists.

    Flattening every JD phrase into one token bag makes a long, detailed JD
    systematically score lower than a short JD. This function scores each
    requirement independently, then averages the results. Unknown requirements
    still receive zero and are never treated as negative candidate facts.
    """

    present = text_tokens(corpus)
    scores: list[float] = []
    for item in needles:
        required = text_tokens(compact(item))
        if required:
            scores.append(len(required & present) / len(required))
    return sum(scores) / len(scores) if scores else 0.0


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


def name_similarity(left: str, right: str) -> float:
    a, b = normalized_name(left), normalized_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def source_domain(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().removeprefix("www.")


def json_load(path: str | None, default: Any) -> Any:
    if not path:
        return default
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


@dataclass
class RateLimitedHTTP:
    min_interval: float = 1.2
    max_retries: int = 2
    timeout: float = 25.0
    last_request: dict[str, float] = field(default_factory=dict)
    request_log: list[dict[str, Any]] = field(default_factory=list)

    def _wait_for_host(self, url: str) -> None:
        host = source_domain(url)
        elapsed = time.monotonic() - self.last_request.get(host, 0.0)
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_request[host] = time.monotonic()

    def json_request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "academic-talent-sourcing/1.0 (public-research-enrichment)",
            **(headers or {}),
        }
        for attempt in range(self.max_retries + 1):
            self._wait_for_host(url)
            started = time.monotonic()
            status: int | None = None
            try:
                request = Request(url, data=body, method=method, headers=request_headers)
                with urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    payload = json.loads(response.read().decode("utf-8", errors="replace"))
                self.request_log.append(
                    {
                        "url": url.split("?", 1)[0],
                        "host": source_domain(url),
                        "status": status,
                        "attempt": attempt + 1,
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                    }
                )
                return payload
            except HTTPError as exc:
                status = exc.code
                retryable = status == 429 or 500 <= status <= 599
                self.request_log.append(
                    {
                        "url": url.split("?", 1)[0],
                        "host": source_domain(url),
                        "status": status,
                        "attempt": attempt + 1,
                        "error": "http_error",
                    }
                )
                if not retryable or attempt >= self.max_retries:
                    raise
                retry_after = safe_int(exc.headers.get("Retry-After"))
                time.sleep(min(30.0, float(retry_after or (2 ** attempt))))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                self.request_log.append(
                    {
                        "url": url.split("?", 1)[0],
                        "host": source_domain(url),
                        "status": status,
                        "attempt": attempt + 1,
                        "error": type(exc).__name__,
                    }
                )
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(8.0, float(2 ** attempt)))
        raise RuntimeError("unreachable")


def record(
    source: str,
    tier: int,
    kind: str,
    url: str,
    title: str,
    data: dict[str, Any],
    snippet: str = "",
) -> dict[str, Any]:
    return {
        "evidence_id": hashlib.sha256(
            f"{source}|{url}|{title}".encode("utf-8")
        ).hexdigest()[:20],
        "source": source,
        "source_tier": tier,
        "kind": kind,
        "url": url,
        "title": compact(title),
        "snippet": compact(snippet)[:2000],
        "data": data,
        "public": True,
        "retrieved_at": utc_now(),
    }


def local_identity_score(query: dict[str, str], item: dict[str, Any]) -> float:
    names = [item.get("name"), item.get("display_name"), item.get("title")]
    best_name = max((name_similarity(query["name"], compact(name)) for name in names), default=0)
    institution_corpus = " ".join(compact(x) for x in as_list(item.get("institutions")))
    institution_score = overlap_ratio([query["institution"]], institution_corpus)
    topic_corpus = " ".join(compact(x) for x in as_list(item.get("topics")))
    topic_score = overlap_ratio([query["research_direction"]], topic_corpus)
    return round(best_name * 0.58 + institution_score * 0.27 + topic_score * 0.15, 4)


def openalex_provider(
    query: dict[str, str],
    http: RateLimitedHTTP,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    key_env = compact(config.get("api_key_env")) or "OPENALEX_API_KEY"
    api_key = os.getenv(key_env)
    if not api_key:
        raise ValueError(f"missing_environment_variable:{key_env}")
    params = {"search": query["name"], "per_page": 8, "api_key": api_key}
    contact = compact(config.get("contact_email"))
    if contact:
        params["mailto"] = contact
    payload = http.json_request("https://api.openalex.org/authors?" + urlencode(params))
    candidates: list[dict[str, Any]] = []
    raw_results = as_list(payload.get("results"))
    for item in raw_results:
        institutions = unique_strings(
            institution.get("display_name")
            for institution in as_list(item.get("last_known_institutions"))
            if isinstance(institution, dict)
        )
        topics = unique_strings(
            topic.get("display_name")
            for topic in as_list(item.get("topics"))
            if isinstance(topic, dict)
        )
        affiliations = []
        for affiliation in as_list(item.get("affiliations")):
            if not isinstance(affiliation, dict):
                continue
            institution = affiliation.get("institution") or {}
            affiliations.append(
                {
                    "institution": compact(institution.get("display_name")),
                    "years": sorted(
                        [year for year in as_list(affiliation.get("years")) if safe_int(year)],
                        reverse=True,
                    ),
                    "source_url": item.get("id"),
                }
            )
        data = {
            "name": item.get("display_name"),
            "orcid": item.get("orcid"),
            "institutions": institutions,
            "topics": topics,
            "affiliations": affiliations,
            "metrics": {
                "works_count": safe_int(item.get("works_count")),
                "citations": safe_int(item.get("cited_by_count")),
                "h_index": safe_int((item.get("summary_stats") or {}).get("h_index")),
            },
            "ids": item.get("ids") or {},
        }
        data["identity_score"] = local_identity_score(query, data)
        candidates.append(
            record(
                "openalex",
                2,
                "author_profile",
                compact(item.get("id")),
                compact(item.get("display_name")),
                data,
            )
        )
    if not candidates:
        return []

    # A fixed enrichment request for the deterministic top result. The query is
    # unchanged; this is not Agent-style semantic retry.
    selected = max(candidates, key=lambda value: value["data"]["identity_score"])
    author_id = selected["url"].rsplit("/", 1)[-1]
    works_params = {
        "filter": f"authorships.author.id:{author_id}",
        "per_page": 25,
        "sort": "-publication_date",
        "api_key": api_key,
    }
    if contact:
        works_params["mailto"] = contact
    works_payload = http.json_request(
        "https://api.openalex.org/works?" + urlencode(works_params)
    )
    works: list[dict[str, Any]] = []
    coauthors: dict[str, dict[str, Any]] = {}
    grants: list[dict[str, Any]] = []
    for work in as_list(works_payload.get("results")):
        work_item = {
            "title": compact(work.get("display_name")),
            "year": safe_int(work.get("publication_year")),
            "doi": compact(work.get("doi")) or None,
            "citations": safe_int(work.get("cited_by_count")),
            "url": compact(work.get("id")),
            "topic": compact((work.get("primary_topic") or {}).get("display_name")),
        }
        works.append(work_item)
        for authorship in as_list(work.get("authorships")):
            author = authorship.get("author") or {}
            coauthor_id = compact(author.get("id"))
            if not coauthor_id or coauthor_id.endswith(author_id):
                continue
            entry = coauthors.setdefault(
                coauthor_id,
                {
                    "name": compact(author.get("display_name")),
                    "author_id": coauthor_id,
                    "joint_works": 0,
                    "source_url": compact(work.get("id")),
                },
            )
            entry["joint_works"] += 1
        for grant in as_list(work.get("grants")):
            if isinstance(grant, dict):
                grants.append(
                    {
                        "funder": compact(grant.get("funder_display_name")),
                        "award_id": compact(grant.get("award_id")) or None,
                        "source_url": compact(work.get("id")),
                    }
                )
    selected["data"]["works"] = works
    selected["data"]["coauthors"] = sorted(
        coauthors.values(), key=lambda item: item["joint_works"], reverse=True
    )[:40]
    selected["data"]["grants"] = grants
    return candidates


def crossref_provider(
    query: dict[str, str],
    http: RateLimitedHTTP,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    params = {
        "query.author": query["name"],
        "query.affiliation": query["institution"],
        "query": query["research_direction"],
        "rows": 15,
        "select": "DOI,title,author,container-title,published,is-referenced-by-count,funder,URL",
    }
    contact = compact(config.get("contact_email"))
    headers = {"User-Agent": f"academic-talent-sourcing/1.0 (mailto:{contact})"} if contact else {}
    payload = http.json_request(
        "https://api.crossref.org/works?" + urlencode(params), headers=headers
    )
    results: list[dict[str, Any]] = []
    for item in as_list((payload.get("message") or {}).get("items")):
        author_names = []
        affiliations = []
        for author in as_list(item.get("author")):
            if not isinstance(author, dict):
                continue
            name = compact(f"{author.get('given', '')} {author.get('family', '')}")
            author_names.append(name)
            affiliations.extend(
                compact(aff.get("name"))
                for aff in as_list(author.get("affiliation"))
                if isinstance(aff, dict)
            )
        title = compact(" ".join(as_list(item.get("title"))))
        funders = [
            {
                "funder": compact(funder.get("name")),
                "award_ids": unique_strings(funder.get("award")),
                "source_url": compact(item.get("URL")),
            }
            for funder in as_list(item.get("funder"))
            if isinstance(funder, dict)
        ]
        results.append(
            record(
                "crossref",
                2,
                "publication",
                compact(item.get("URL")) or f"https://doi.org/{item.get('DOI', '')}",
                title,
                {
                    "authors": author_names,
                    "institutions": unique_strings(affiliations),
                    "topics": [query["research_direction"]],
                    "doi": compact(item.get("DOI")) or None,
                    "citations": safe_int(item.get("is-referenced-by-count")),
                    "journal": compact(" ".join(as_list(item.get("container-title")))),
                    "funders": funders,
                },
            )
        )
    return results


def semantic_scholar_provider(
    query: dict[str, str],
    http: RateLimitedHTTP,
    provider: dict[str, Any],
) -> list[dict[str, Any]]:
    fields = "name,affiliations,paperCount,citationCount,hIndex,papers.title,papers.year,papers.citationCount,papers.url,papers.authors"
    url = "https://api.semanticscholar.org/graph/v1/author/search?" + urlencode(
        {"query": query["name"], "limit": 8, "fields": fields}
    )
    headers = {}
    key = os.getenv(compact(provider.get("api_key_env")) or "S2_API_KEY")
    if key:
        headers["x-api-key"] = key
    payload = http.json_request(url, headers=headers)
    results: list[dict[str, Any]] = []
    for item in as_list(payload.get("data")):
        coauthors: dict[str, dict[str, Any]] = {}
        works = []
        for paper in as_list(item.get("papers")):
            works.append(
                {
                    "title": compact(paper.get("title")),
                    "year": safe_int(paper.get("year")),
                    "citations": safe_int(paper.get("citationCount")),
                    "url": compact(paper.get("url")),
                }
            )
            for author in as_list(paper.get("authors")):
                author_id = compact(author.get("authorId"))
                if not author_id or author_id == compact(item.get("authorId")):
                    continue
                entry = coauthors.setdefault(
                    author_id,
                    {
                        "name": compact(author.get("name")),
                        "author_id": author_id,
                        "joint_works": 0,
                        "source_url": compact(paper.get("url")),
                    },
                )
                entry["joint_works"] += 1
        data = {
            "name": compact(item.get("name")),
            "institutions": unique_strings(item.get("affiliations")),
            "topics": [],
            "metrics": {
                "works_count": safe_int(item.get("paperCount")),
                "citations": safe_int(item.get("citationCount")),
                "h_index": safe_int(item.get("hIndex")),
            },
            "works": works,
            "coauthors": sorted(
                coauthors.values(), key=lambda entry: entry["joint_works"], reverse=True
            )[:40],
        }
        data["identity_score"] = local_identity_score(query, data)
        results.append(
            record(
                "semantic_scholar",
                2,
                "author_profile",
                f"https://www.semanticscholar.org/author/{item.get('authorId', '')}",
                data["name"],
                data,
            )
        )
    return results


def orcid_provider(
    query: dict[str, str],
    http: RateLimitedHTTP,
    provider: dict[str, Any],
) -> list[dict[str, Any]]:
    token_env = compact(provider.get("access_token_env")) or "ORCID_ACCESS_TOKEN"
    access_token = os.getenv(token_env)
    if not access_token:
        raise ValueError(f"missing_environment_variable:{token_env}")
    search = (
        f'given-and-family-names:"{query["name"]}" AND '
        f'affiliation-org-name:"{query["institution"]}"'
    )
    url = "https://pub.orcid.org/v3.0/expanded-search/?" + urlencode(
        {"q": search, "rows": 10}
    )
    payload = http.json_request(
        url,
        headers={
            "Accept": "application/vnd.orcid+json",
            "Authorization": f"Bearer {access_token}",
        },
    )
    results = []
    for item in as_list(payload.get("expanded-result")):
        name = compact(
            f"{item.get('given-names', '')} {item.get('family-names', '')}"
        )
        orcid = compact(item.get("orcid-id"))
        institutions = unique_strings(
            as_list(item.get("institution-name")) + as_list(item.get("other-name"))
        )
        data = {
            "name": name,
            "institutions": institutions,
            "topics": [],
            "orcid": orcid,
        }
        data["identity_score"] = local_identity_score(query, data)
        results.append(
            record(
                "orcid",
                2,
                "author_profile",
                f"https://orcid.org/{orcid}",
                name,
                data,
            )
        )
    return results


def brave_provider(
    query: dict[str, str],
    http: RateLimitedHTTP,
    provider: dict[str, Any],
) -> list[dict[str, Any]]:
    key_env = compact(provider.get("api_key_env")) or "BRAVE_SEARCH_API_KEY"
    api_key = os.getenv(key_env)
    if not api_key:
        raise ValueError(f"missing_environment_variable:{key_env}")
    fixed_queries = [
        (1, "official", f'"{query["name"]}" "{query["institution"]}" "{query["research_direction"]}" laboratory department'),
        (2, "google_scholar", f'site:scholar.google.com/citations "{query["name"]}" "{query["institution"]}"'),
        (3, "researchgate", f'site:researchgate.net/profile "{query["name"]}" "{query["research_direction"]}"'),
        (4, "linkedin", f'site:linkedin.com/in "{query["name"]}" "{query["institution"]}"'),
        (5, "conference", f'"{query["name"]}" "{query["research_direction"]}" conference keynote seminar lecture'),
    ]
    records: list[dict[str, Any]] = []
    for tier, category, search_query in fixed_queries:
        url = "https://api.search.brave.com/res/v1/web/search?" + urlencode(
            {"q": search_query, "count": 10, "safesearch": "moderate"}
        )
        payload = http.json_request(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        )
        for item in as_list((payload.get("web") or {}).get("results")):
            records.append(
                record(
                    "brave_search",
                    tier,
                    f"web_{category}",
                    compact(item.get("url")),
                    compact(item.get("title")),
                    {
                        "query_category": category,
                        "name": query["name"],
                        "institutions": [query["institution"]],
                        "topics": [query["research_direction"]],
                    },
                    compact(item.get("description")),
                )
            )
    return records


def load_fixture(path: str) -> list[dict[str, Any]]:
    payload = json_load(path, {})
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("Fixture must be a list or an object with a records list.")
    normalized = []
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            continue
        clone = dict(item)
        clone.setdefault("evidence_id", f"fixture-{index + 1}")
        clone.setdefault("source", "fixture")
        clone.setdefault("source_tier", 2)
        clone.setdefault("kind", "web_official")
        clone.setdefault("url", f"fixture://record/{index + 1}")
        clone.setdefault("title", "")
        clone.setdefault("snippet", "")
        clone.setdefault("data", {})
        clone.setdefault("public", True)
        clone.setdefault("retrieved_at", utc_now())
        normalized.append(clone)
    return normalized


def collect_provider_records(
    query: dict[str, str],
    config: dict[str, Any],
    fixture_path: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    http = RateLimitedHTTP(
        min_interval=float(config.get("min_interval_seconds", 1.2)),
        max_retries=int(config.get("max_transport_retries", 2)),
        timeout=float(config.get("timeout_seconds", 25.0)),
    )
    records = load_fixture(fixture_path) if fixture_path else []
    failures: list[dict[str, Any]] = []
    include_network = not fixture_path or bool(config.get("include_network_with_fixture"))
    providers = config.get("providers") or {}
    dispatch = [
        ("openalex", openalex_provider, False),
        ("crossref", crossref_provider, True),
        ("orcid", orcid_provider, False),
        ("semantic_scholar", semantic_scholar_provider, False),
        ("brave", brave_provider, False),
    ]
    if include_network:
        for name, function, default_enabled in dispatch:
            provider = providers.get(name) or {}
            if not bool(provider.get("enabled", default_enabled)):
                continue
            try:
                provider_config = {**config, **provider}
                records.extend(function(query, http, provider_config))
            except Exception as exc:
                failures.append(
                    {
                        "provider": name,
                        "error": type(exc).__name__,
                        "message": compact(str(exc))[:300],
                    }
                )
    return records, failures, http.request_log


def record_corpus(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    return " ".join(
        [
            compact(item.get("title")),
            compact(item.get("snippet")),
            compact(data.get("name")),
            " ".join(unique_strings(data.get("institutions"))),
            " ".join(unique_strings(data.get("topics"))),
        ]
    )


def evidence_identity_score(query: dict[str, str], item: dict[str, Any]) -> float:
    data = item.get("data") or {}
    if data.get("identity_score") is not None:
        return float(data["identity_score"])
    corpus = record_corpus(item)
    name_score = name_similarity(query["name"], compact(data.get("name") or item.get("title")))
    institution_score = overlap_ratio([query["institution"]], corpus)
    direction_score = overlap_ratio([query["research_direction"]], corpus)
    return round(name_score * 0.58 + institution_score * 0.27 + direction_score * 0.15, 4)


def identity_analysis(
    query: dict[str, str], records: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in records:
        if item.get("kind") != "author_profile":
            continue
        data = item.get("data") or {}
        name = compact(data.get("name") or item.get("title"))
        institutions = unique_strings(data.get("institutions"))
        institution_key = normalized_name(institutions[0]) if institutions else "unknown"
        group_key = f"{normalized_name(name)}|{institution_key}"
        candidate = grouped.setdefault(
            group_key,
            {
                "name": name,
                "institutions": [],
                "topics": [],
                "sources": [],
                "source_urls": [],
                "evidence_ids": [],
                "scores": [],
            },
        )
        candidate["institutions"].extend(institutions)
        candidate["topics"].extend(unique_strings(data.get("topics")))
        candidate["sources"].append(item.get("source"))
        candidate["source_urls"].append(item.get("url"))
        candidate["evidence_ids"].append(item.get("evidence_id"))
        candidate["scores"].append(evidence_identity_score(query, item))
    candidates = []
    for candidate in grouped.values():
        scores = candidate.pop("scores")
        candidate["institutions"] = unique_strings(candidate["institutions"])
        candidate["topics"] = unique_strings(candidate["topics"])
        candidate["sources"] = unique_strings(candidate["sources"])
        candidate["source_urls"] = unique_strings(candidate["source_urls"])
        candidate["evidence_ids"] = unique_strings(candidate["evidence_ids"])
        candidate["score"] = round(
            min(1.0, max(scores) + min(0.05, 0.02 * (len(candidate["sources"]) - 1))),
            4,
        )
        candidate["evidence_id"] = candidate["evidence_ids"][0]
        candidate["source"] = candidate["sources"][0]
        candidate["source_url"] = candidate["source_urls"][0]
        candidates.append(candidate)
    candidates.sort(key=lambda value: value["score"], reverse=True)
    top = candidates[0] if candidates else None
    runner_up = candidates[1] if len(candidates) > 1 else None
    domains = {
        source_domain(item.get("url", ""))
        for item in records
        if evidence_identity_score(query, item) >= 0.6 and source_domain(item.get("url", ""))
    }
    if not top or top["score"] < 0.48:
        ambiguity = "high"
    elif runner_up and runner_up["score"] >= top["score"] - 0.08:
        ambiguity = "high"
    elif top["score"] < 0.68 or len(domains) < 2:
        ambiguity = "medium"
    else:
        ambiguity = "low"
    identity = {
        "query_name": query["name"],
        "best_match": top,
        "candidates": candidates[:10],
        "ambiguity": ambiguity,
        "cross_verified_domains": sorted(domains),
        "cross_verified": len(domains) >= 2 and ambiguity != "high",
    }
    return identity, candidates


def relevant_records(
    query: dict[str, str],
    identity: dict[str, Any],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    best_ids = set((identity.get("best_match") or {}).get("evidence_ids") or [])
    selected = []
    for item in records:
        score = evidence_identity_score(query, item)
        if item.get("kind") == "author_profile":
            if item.get("evidence_id") in best_ids:
                selected.append(item)
        elif score >= 0.55:
            selected.append(item)
    return selected


def aggregate_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    best: dict[str, dict[str, Any]] = {}
    for item in records:
        metrics = (item.get("data") or {}).get("metrics") or {}
        for key in ("h_index", "citations", "works_count"):
            value = safe_int(metrics.get(key))
            if value is None:
                continue
            current = best.get(key)
            if current is None or value > current["value"]:
                best[key] = {
                    "value": value,
                    "source": item.get("source"),
                    "source_url": item.get("url"),
                    "retrieved_at": item.get("retrieved_at"),
                }
    return {key: best.get(key) for key in ("citations", "h_index", "works_count")}


def aggregate_profile_parts(records: list[dict[str, Any]]) -> dict[str, Any]:
    institutions: list[str] = []
    topics: list[str] = []
    affiliations: list[dict[str, Any]] = []
    works: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    coauthors: dict[str, dict[str, Any]] = {}
    public_talks: list[dict[str, Any]] = []
    for item in records:
        data = item.get("data") or {}
        institutions.extend(as_list(data.get("institutions")))
        topics.extend(as_list(data.get("topics")))
        affiliations.extend(
            affiliation for affiliation in as_list(data.get("affiliations"))
            if isinstance(affiliation, dict)
        )
        works.extend(
            work for work in as_list(data.get("works")) if isinstance(work, dict)
        )
        grants.extend(
            grant for grant in as_list(data.get("grants")) if isinstance(grant, dict)
        )
        grants.extend(
            grant for grant in as_list(data.get("funders")) if isinstance(grant, dict)
        )
        for collaborator in as_list(data.get("coauthors")):
            if not isinstance(collaborator, dict):
                continue
            key = compact(collaborator.get("author_id") or collaborator.get("name")).casefold()
            if not key:
                continue
            existing = coauthors.setdefault(key, dict(collaborator))
            existing["joint_works"] = max(
                safe_int(existing.get("joint_works")) or 0,
                safe_int(collaborator.get("joint_works")) or 0,
            )
        if item.get("kind") == "web_conference":
            public_talks.append(
                {
                    "title": item.get("title"),
                    "summary": item.get("snippet"),
                    "source_url": item.get("url"),
                    "retrieved_at": item.get("retrieved_at"),
                }
            )
        if data.get("public_talk"):
            public_talks.extend(as_list(data.get("public_talk")))
    deduped_works: dict[str, dict[str, Any]] = {}
    for work in works:
        key = compact(work.get("doi") or work.get("url") or work.get("title")).casefold()
        if key:
            deduped_works.setdefault(key, work)
    return {
        "institutions": unique_strings(institutions),
        "topics": unique_strings(topics),
        "career": affiliations,
        "works": list(deduped_works.values())[:50],
        "grants": grants[:40],
        "collaboration_graph": {
            "nodes": sorted(
                coauthors.values(),
                key=lambda value: safe_int(value.get("joint_works")) or 0,
                reverse=True,
            )[:40],
            "edge_definition": "public coauthorship with the candidate",
        },
        "public_talks": public_talks[:20],
    }


def extract_contacts(records: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    public_contacts: dict[str, dict[str, Any]] = {}
    departmental: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for item in records:
        if item.get("public") is not True or not item.get("url"):
            continue
        data = item.get("data") or {}
        candidates = EMAIL_RE.findall(
            " ".join(
                [
                    compact(item.get("snippet")),
                    compact(data.get("email")),
                    " ".join(unique_strings(data.get("emails"))),
                ]
            )
        )
        for email in candidates:
            normalized = email.casefold()
            domain = normalized.split("@", 1)[1]
            if domain in PRIVATE_EMAIL_DOMAINS:
                rejected.append(f"{normalized}:private_domain")
                continue
            contact = {
                "email": normalized,
                "source_url": item.get("url"),
                "source": item.get("source"),
                "retrieved_at": item.get("retrieved_at"),
                "publicly_listed": True,
            }
            local = normalized.split("@", 1)[0]
            if local in DEPARTMENTAL_LOCALS or any(
                token in local for token in ("admin", "office", "dept", "secretary")
            ):
                departmental.setdefault(normalized, contact)
            else:
                public_contacts.setdefault(normalized, contact)
    return {
        "public_institutional": list(public_contacts.values()),
        "departmental": list(departmental.values()),
        "rejected_count": len(rejected),
    }, rejected


def collect_homepages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect public official/lab/institution profile pages without deciding identity."""
    collected: dict[str, dict[str, Any]] = {}
    for item in records:
        if item.get("public") is not True:
            continue
        data = item.get("data") or {}
        urls = []
        for key in ("homepage", "homepage_url", "profile_url", "personal_website"):
            urls.extend(unique_strings(as_list(data.get(key))))
        if item.get("kind") == "author_profile" and safe_int(item.get("source_tier")) == 1:
            urls.extend(unique_strings([item.get("url")]))
        for url in unique_strings(urls):
            if not url.lower().startswith(("http://", "https://")):
                continue
            collected.setdefault(
                url,
                {
                    "url": url,
                    "source": item.get("source"),
                    "source_url": item.get("url"),
                    "retrieved_at": item.get("retrieved_at"),
                    "publicly_listed": True,
                    "official_or_institutional": safe_int(item.get("source_tier")) == 1,
                    "evidence_id": item.get("evidence_id"),
                },
            )
    return list(collected.values())


def career_and_mobility(
    career: list[dict[str, Any]],
    grants: list[dict[str, Any]],
    records: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    today = date.today()
    signals: list[dict[str, Any]] = []
    recent_join = False
    for item in career:
        start = parse_iso_date(item.get("start_date"))
        if start and timedelta(0) <= today - start <= timedelta(days=180):
            recent_join = True
            signals.append(
                {
                    "type": "recent_join",
                    "date": start.isoformat(),
                    "strength": "hold",
                    "source_url": item.get("source_url"),
                }
            )
    ending_days: list[int] = []
    for grant in grants:
        end = parse_iso_date(grant.get("end_date"))
        if end and today <= end <= today + timedelta(days=365):
            days = (end - today).days
            ending_days.append(days)
            signals.append(
                {
                    "type": "grant_near_end",
                    "days": days,
                    "strength": "high" if days <= 180 else "medium",
                    "source_url": grant.get("source_url"),
                }
            )
    for item in records:
        data = item.get("data") or {}
        if data.get("verified_funding_decrease") is True:
            signals.append(
                {
                    "type": "verified_funding_decrease",
                    "strength": "medium",
                    "source_url": item.get("url"),
                }
            )
        if data.get("public_move_signal") is True:
            signals.append(
                {
                    "type": "public_move_signal",
                    "strength": "high",
                    "source_url": item.get("url"),
                }
            )
    crm = context.get("authorized_crm_signals") or {}
    in_flight_offer = crm.get("in_flight_offer") is True
    if in_flight_offer:
        signals.append(
            {
                "type": "authorized_crm_in_flight_offer",
                "strength": "hold",
                "source_url": None,
            }
        )
    hold = recent_join or in_flight_offer
    if hold:
        window = "low"
    elif any(signal["strength"] == "high" for signal in signals):
        window = "high"
    elif any(signal["strength"] == "medium" for signal in signals):
        window = "medium"
    else:
        window = "low"
    return {
        "window": window,
        "recent_join": recent_join,
        "in_flight_offer_from_authorized_crm": in_flight_offer,
        "hold_recommended": hold,
        "signals": signals,
        "interpretation": "inference_for_outreach_timing_not_candidate_intent",
    }


def academic_tags(metrics: dict[str, Any], corpus: str) -> list[str]:
    tokens = text_tokens(corpus)
    h_index = ((metrics.get("h_index") or {}).get("value")) or 0
    works = ((metrics.get("works_count") or {}).get("value")) or 0
    tags = []
    if tokens & LEADERSHIP_TERMS or h_index >= 30 or works >= 80:
        tags.append("实验室带头人")
    elif h_index >= 8 or works >= 15:
        tags.append("青年潜力学者")
    if tokens & TRANSLATION_TERMS:
        tags.append("产业落地研究员")
    return tags or ["待补充证据"]


def jd_match(
    jd: dict[str, Any],
    profile_corpus: str,
) -> dict[str, Any]:
    if not jd:
        return {
            "score": None,
            "components": {},
            "strengths": [],
            "gaps": [],
            "status": "not_requested",
        }
    dimensions = [
        ("research_fit", 35, as_list(jd.get("research_directions")) + as_list(jd.get("must_have"))),
        ("methods", 20, as_list(jd.get("methods")) + as_list(jd.get("technical_skills"))),
        ("seniority", 15, [jd.get("level"), jd.get("seniority")]),
        # The JD lists are requirements. TRANSLATION_TERMS and LEADERSHIP_TERMS
        # are synonym dictionaries for tagging, not dozens of additional
        # mandatory requirements, so they must not dilute the score here.
        ("translation", 15, as_list(jd.get("industry_translation"))),
        ("leadership", 10, as_list(jd.get("leadership"))),
        ("location", 5, [jd.get("location"), jd.get("work_mode")]),
    ]
    labels = {
        "research_fit": "研究方向",
        "methods": "方法/技术",
        "seniority": "职级与资历",
        "translation": "产业转化",
        "leadership": "团队/项目领导力",
        "location": "地域可行性",
    }
    components: dict[str, dict[str, Any]] = {}
    strengths: list[str] = []
    gaps: list[str] = []
    score = 0.0
    for key, weight, terms in dimensions:
        cleaned_terms = [term for term in terms if compact(term)]
        ratio = dimension_coverage(cleaned_terms, profile_corpus) if cleaned_terms else 0.0
        points = round(weight * min(1.0, ratio), 2)
        score += points
        matched = sorted(text_tokens(" ".join(compact(term) for term in cleaned_terms)) & text_tokens(profile_corpus))
        components[key] = {
            "weight": weight,
            "points": points,
            "coverage": round(ratio, 3),
            "matched_terms": matched[:20],
        }
        if ratio >= 0.5:
            strengths.append(f"{labels[key]}证据较强")
        elif cleaned_terms:
            gaps.append(f"{labels[key]}证据不足")
    preferred = as_list(jd.get("preferred"))
    preferred_ratio = dimension_coverage(preferred, profile_corpus)
    # Preferred terms refine evidence notes but do not exceed the fixed 100 points.
    if preferred and preferred_ratio >= 0.5:
        strengths.append("优选条件有公开证据支持")
    elif preferred:
        gaps.append("优选条件需面谈核验")
    return {
        "score": int(round(min(100.0, score))),
        "components": components,
        "strengths": strengths,
        "gaps": gaps,
        "status": "evidence_based",
        "note": "Unknown information receives no points; it is not treated as a negative fact.",
    }


def salary_reference(
    benchmarks: list[dict[str, Any]],
    jd: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    today = date.today()
    region_tokens = text_tokens(
        " ".join(
            [
                compact(jd.get("location")),
                " ".join(profile.get("institutions") or []),
            ]
        )
    )
    role_tokens = text_tokens(compact(jd.get("title")))
    valid = []
    for item in benchmarks:
        if not isinstance(item, dict) or not item.get("source_url"):
            continue
        as_of = parse_iso_date(item.get("as_of"))
        if not as_of or (today - as_of).days > 730:
            continue
        score = 0
        score += len(region_tokens & text_tokens(item.get("region"))) * 2
        score += len(role_tokens & text_tokens(item.get("role")))
        valid.append((score, item))
    if not valid:
        return {
            "status": "needs_verified_benchmark",
            "range": None,
            "note": "Do not estimate or promise compensation without dated source data.",
        }
    _, selected = max(valid, key=lambda pair: pair[0])
    return {
        "status": "verified_input_benchmark",
        "range": {
            "min": selected.get("min"),
            "max": selected.get("max"),
            "currency": selected.get("currency"),
            "period": selected.get("period", "annual"),
            "region": selected.get("region"),
            "role": selected.get("role"),
        },
        "source_url": selected.get("source_url"),
        "as_of": selected.get("as_of"),
    }


def collect_outreach_anchors(
    context: dict[str, Any],
    parts: dict[str, Any],
) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    paper = context.get("paper") or {}
    if compact(paper.get("title")):
        anchors.append(
            {
                "type": "paper",
                "title": compact(paper.get("title")),
                "summary": compact(paper.get("summary")) or None,
                "source_url": compact(paper.get("url") or paper.get("doi")) or None,
            }
        )
    for talk in parts["public_talks"]:
        anchors.append(
            {
                "type": "public_talk",
                "title": compact(talk.get("title")),
                "summary": compact(talk.get("summary")) or None,
                "source_url": compact(talk.get("source_url")),
            }
        )
    for work in parts["works"]:
        anchors.append(
            {
                "type": "paper",
                "title": compact(work.get("title")),
                "summary": compact(work.get("topic")) or None,
                "source_url": compact(work.get("url") or work.get("doi")) or None,
            }
        )
    deduped: dict[str, dict[str, Any]] = {}
    for anchor in anchors:
        key = compact(anchor.get("source_url") or anchor.get("title")).casefold()
        if key and compact(anchor.get("title")):
            deduped.setdefault(key, anchor)
    return list(deduped.values())[:20]


def completeness(
    query: dict[str, str],
    identity: dict[str, Any],
    metrics: dict[str, Any],
    parts: dict[str, Any],
    contacts: dict[str, Any],
    homepages: list[dict[str, Any]],
    anchor: dict[str, Any] | None,
) -> dict[str, Any]:
    checks = {
        "verified_identity": identity.get("cross_verified") is True,
        "institution": bool(parts["institutions"] or query["institution"]),
        "research_direction": bool(parts["topics"] or query["research_direction"]),
        "academic_metrics": bool(metrics.get("h_index") or metrics.get("citations")),
        "works": bool(parts["works"]),
        "career": bool(parts["career"]),
        "public_contact_or_department": bool(
            contacts["public_institutional"] or contacts["departmental"]
        ),
        "homepage": bool(homepages),
        "outreach_anchor": bool(anchor),
    }
    missing = [key for key, present in checks.items() if not present]
    return {
        "score": round(sum(checks.values()) / len(checks), 3),
        "missing_fields": missing,
        "checks": checks,
    }


def analyze(
    payload: dict[str, Any],
    fresh_records: list[dict[str, Any]],
    provider_failures: list[dict[str, Any]],
    request_log: list[dict[str, Any]],
) -> dict[str, Any]:
    query = payload["query"]
    context = payload.get("evaluation_context") or {}
    prior = [
        item for item in as_list(context.get("prior_evidence"))
        if isinstance(item, dict)
    ]
    deduped: dict[str, dict[str, Any]] = {}
    for item in prior + fresh_records:
        key = compact(item.get("evidence_id") or item.get("url"))
        if key:
            deduped.setdefault(key, item)
    all_records = list(deduped.values())
    identity, _ = identity_analysis(query, all_records)
    relevant = relevant_records(query, identity, all_records)
    metrics = aggregate_metrics(relevant)
    parts = aggregate_profile_parts(relevant)
    if not parts["institutions"] and query["institution"]:
        parts["institutions"] = [query["institution"]]
    if not parts["topics"] and query["research_direction"]:
        parts["topics"] = [query["research_direction"]]
    contacts, rejected_contacts = extract_contacts(relevant)
    homepages = collect_homepages(relevant)
    mobility = career_and_mobility(
        parts["career"], parts["grants"], relevant, context
    )
    corpus = " ".join(
        [
            query["research_direction"],
            " ".join(parts["institutions"]),
            " ".join(parts["topics"]),
            " ".join(record_corpus(item) for item in relevant),
            " ".join(compact(work.get("title")) for work in parts["works"]),
        ]
    )
    tags = academic_tags(metrics, corpus)
    match = jd_match(context.get("jd") or {}, corpus)
    salary = salary_reference(
        as_list(context.get("salary_benchmarks")),
        context.get("jd") or {},
        parts,
    )
    anchors = collect_outreach_anchors(context, parts)
    anchor = anchors[0] if anchors else None
    complete = completeness(
        query, identity, metrics, parts, contacts, homepages, anchor
    )
    risk_flags = []
    if identity["ambiguity"] == "high":
        risk_flags.append("identity_ambiguity_high")
    if rejected_contacts:
        risk_flags.append("private_or_unqualified_email_filtered")
    if provider_failures:
        risk_flags.append("provider_partial_failure")
    if not all_records:
        risk_flags.append("no_public_evidence")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "candidate_key": payload.get("candidate_key"),
        "query": query,
        "identity": identity,
        "profile": {
            "name": compact((identity.get("best_match") or {}).get("name")) or query["name"],
            "institutions": parts["institutions"],
            "research_directions": parts["topics"],
            "academic_metrics": metrics,
            "academic_tags": tags,
            "career": parts["career"],
            "works": parts["works"],
            "grants": parts["grants"],
            "public_talks": parts["public_talks"],
            "collaboration_graph": parts["collaboration_graph"],
            "contacts": contacts,
            "homepages": homepages,
            "outreach_anchor": anchor,
            "outreach_anchors": anchors,
        },
        "jd_match": match,
        "mobility": mobility,
        "salary_reference": salary,
        "completeness": complete,
        "evidence": sorted(
            all_records,
            key=lambda item: (safe_int(item.get("source_tier")) or 99, item.get("source", "")),
        ),
        "provider_failures": provider_failures,
        "request_log": request_log,
        "risk_flags": risk_flags,
        "filtered_contact_reasons": rejected_contacts,
        "agent_next_action_fields": {
            "ambiguity": identity["ambiguity"],
            "missing_fields": complete["missing_fields"],
            "hold_recommended": mobility["hold_recommended"],
            "jd_score": match["score"],
        },
    }


def validate_input(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object.")
    query = payload.get("query")
    if not isinstance(query, dict):
        raise ValueError("Input must contain a query object.")
    allowed = {"name", "institution", "research_direction"}
    extras = set(query) - allowed
    missing = [key for key in allowed if not compact(query.get(key))]
    if extras:
        raise ValueError(f"Query contains disallowed keys: {sorted(extras)}")
    if missing:
        raise ValueError(f"Query missing required values: {sorted(missing)}")
    payload["query"] = {key: compact(query[key]) for key in allowed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded public academic-person search and deterministic assessment."
    )
    parser.add_argument("--input", required=True, help="Candidate query/evaluation JSON.")
    parser.add_argument("--output", required=True, help="Output profile JSON.")
    parser.add_argument("--config", help="Optional provider/rate-limit JSON.")
    parser.add_argument(
        "--fixture",
        help="Offline evidence fixture. Network is disabled unless config opts in.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        payload = json_load(args.input, {})
        validate_input(payload)
        config = json_load(args.config, {})
        records, failures, request_log = collect_provider_records(
            payload["query"], config, args.fixture
        )
        result = analyze(payload, records, failures, request_log)
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "output": str(output_path),
                    "evidence": len(result["evidence"]),
                    "completeness": result["completeness"]["score"],
                    "ambiguity": result["identity"]["ambiguity"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {compact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

