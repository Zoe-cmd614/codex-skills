#!/usr/bin/env python3
"""
所属 Skill：Skill 1 — PDF 解析 Skill
前置依赖：Python 3.11+、pypdf>=5.0；可选 GROBID 服务。
Agent 调用入口：
    python pdf_parser_script.py <paper.pdf|directory> [...] --output parsed.json

边界：本脚本只做批量 PDF 结构化抽取。它不联网检索作者、不判断是否
触达、不计算人才/JD 分数，也不调用另外两个 Skill。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - dependency failure path
    raise SystemExit("Missing dependency: install pypdf>=5.0") from exc


SCHEMA_VERSION = "1.0"
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
ARXIV_RE = re.compile(r"\barXiv:(\d{4}\.\d{4,5}(?:v\d+)?)\b", re.I)
PROJECT_RE = re.compile(
    r"\b(?:grant|project|award|contract|项目|课题)[\s:#：-]*"
    r"([A-Z]{0,10}-?\d[A-Z0-9._/-]{3,})\b",
    re.I,
)
INSTITUTION_WORDS = (
    "university",
    "institute",
    "laboratory",
    "lab ",
    "school of",
    "department of",
    "hospital",
    "college",
    "academy",
    "research center",
    "research centre",
    "大学",
    "学院",
    "研究所",
    "实验室",
    "医院",
    "研究中心",
)
FUNDING_WORDS = (
    "funded by",
    "supported by",
    "funding",
    "grant",
    "financial support",
    "资助",
    "基金",
    "项目支持",
)
STOPWORDS = {
    "with", "from", "that", "this", "were", "have", "using", "based", "study",
    "analysis", "research", "results", "into", "through", "between", "their",
    "paper", "method", "approach", "novel", "effect", "effects", "data",
    "the", "and", "for", "of", "in", "on", "to", "a", "an",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compact(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalized_person_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", value.casefold())


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = compact(value).strip(" ,;")
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def collect_pdf_paths(inputs: list[str], recursive: bool) -> list[Path]:
    paths: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and path.suffix.lower() == ".pdf":
            paths.append(path)
        elif path.is_dir():
            pattern = "**/*.pdf" if recursive else "*.pdf"
            paths.extend(p.resolve() for p in path.glob(pattern) if p.is_file())
        else:
            raise FileNotFoundError(f"Not a PDF file or directory: {path}")
    return sorted(set(paths), key=lambda item: str(item).casefold())


def read_pdf_text(path: Path) -> tuple[str, list[str], bool]:
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("encrypted_pdf")
        except Exception as exc:
            raise ValueError("encrypted_pdf") from exc
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\f\n".join(pages), pages, bool(reader.is_encrypted)


def post_to_grobid(path: Path, grobid_url: str, timeout: float) -> str:
    """Submit one PDF to an explicitly configured GROBID service."""
    boundary = f"----codex-{uuid.uuid4().hex}"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="input"; filename="{path.name}"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8")
    body = header + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    endpoint = grobid_url.rstrip("/") + "/api/processFulltextDocument"
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/xml",
            "User-Agent": "academic-talent-sourcing/1.0",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def xml_text(node: ET.Element | None) -> str:
    return compact(" ".join(node.itertext())) if node is not None else ""


def tei_first(root: ET.Element, path: str) -> ET.Element | None:
    return root.find(path)


def parse_grobid_tei(xml: str, source_pdf: Path) -> dict[str, Any]:
    root = ET.fromstring(xml)
    title_node = tei_first(root, ".//{*}teiHeader/{*}fileDesc/{*}titleStmt/{*}title")
    if title_node is None:
        title_node = tei_first(root, ".//{*}analytic/{*}title")
    title = xml_text(title_node)

    doi = None
    for node in root.findall(".//{*}idno"):
        if (node.attrib.get("type") or "").lower() == "doi":
            doi = compact(node.text).lower()
            break

    journal_node = tei_first(root, ".//{*}monogr/{*}title")
    journal = xml_text(journal_node) or None
    authors: list[dict[str, Any]] = []
    author_nodes = root.findall(".//{*}teiHeader/{*}fileDesc/{*}sourceDesc//{*}analytic/{*}author")
    for index, node in enumerate(author_nodes, start=1):
        forenames = [compact(n.text) for n in node.findall(".//{*}forename") if compact(n.text)]
        surname = compact((node.findtext(".//{*}surname") or ""))
        name = compact(" ".join(forenames + ([surname] if surname else [])))
        affiliations: list[str] = []
        for affiliation in node.findall(".//{*}affiliation"):
            value = xml_text(affiliation)
            if value:
                affiliations.append(value)
        emails = unique(n.text or "" for n in node.findall(".//{*}email"))
        marker = " ".join(node.attrib.values()).lower()
        authors.append(
            {
                "name": name or f"unknown-author-{index}",
                "order": index,
                "is_corresponding": "corresp" in marker,
                "institutions": unique(affiliations),
                "paper_emails": emails,
                "research_directions": [],
                "field_confidence": {
                    "name": 0.96 if name else 0.2,
                    "institutions": 0.9 if affiliations else 0.0,
                    "paper_emails": 0.98 if emails else 0.0,
                },
            }
        )

    keywords = unique(xml_text(node) for node in root.findall(".//{*}keywords/{*}term"))
    funders = unique(
        xml_text(node)
        for node in root.findall(".//{*}orgName")
        if (node.attrib.get("type") or "").lower() in {"funder", "funding"}
    )
    project_ids = unique(
        compact(node.text)
        for node in root.findall(".//{*}idno")
        if "grant" in (node.attrib.get("type") or "").lower()
    )
    funding = [
        {"funder": funder, "project_ids": project_ids, "source": "grobid_tei"}
        for funder in funders
    ]
    for author in authors:
        author["research_directions"] = keywords[:8]

    return {
        "source_pdf": str(source_pdf),
        "paper": {
            "title": title or source_pdf.stem,
            "doi": doi,
            "journal": journal,
            "publication_date": None,
            "research_directions": keywords[:12],
            "funding": funding,
            "unassigned_emails": [],
        },
        "authors": authors,
        "quality": {
            "parser": "grobid",
            "confidence": 0.94 if authors and title else 0.7,
            "warnings": [],
        },
    }


def likely_author_line(lines: list[str]) -> tuple[int | None, str]:
    best: tuple[float, int, str] | None = None
    for index, line in enumerate(lines[:30]):
        cleaned = compact(line)
        lower = cleaned.casefold()
        if (
            not cleaned
            or len(cleaned) > 400
            or any(token in lower for token in ("abstract", "keyword", "doi:", "received"))
            or any(token in lower for token in INSTITUTION_WORDS)
            or EMAIL_RE.search(cleaned)
        ):
            continue
        separators = cleaned.count(",") + cleaned.count(";") + len(re.findall(r"\band\b", cleaned, re.I))
        capital_words = len(re.findall(r"\b[A-Z][A-Za-z'’-]+\b", cleaned))
        markers = len(re.findall(r"[*†‡\d]", cleaned))
        score = separators * 2.0 + capital_words * 0.35 + markers * 0.15
        if 2 <= capital_words <= 40 and separators >= 1:
            candidate = (score, index, cleaned)
            if best is None or candidate[0] > best[0]:
                best = candidate
    return (best[1], best[2]) if best else (None, "")


def marked_author_entries(text: str) -> list[dict[str, Any]]:
    """Parse camera-ready author rows such as 'Alice Smith * 1 Bob Jones2'."""
    pattern = re.compile(
        r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+"
        r"(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+){1,2})"
        r"\s*([*†‡]?)\s*(\d+)(?=\s+[A-Z]|$)"
    )
    entries = []
    for match in pattern.finditer(compact(text)):
        name = compact(match.group(1))
        if any(word in name.casefold() for word in ("university", "department", "school")):
            continue
        entries.append(
            {
                "name": name,
                "marker": match.group(3),
                "equal_contribution": match.group(2) == "*",
            }
        )
    return entries


def find_header_authors(lines: list[str]) -> tuple[int | None, list[dict[str, Any]], str]:
    abstract_index = next(
        (index for index, line in enumerate(lines[:50]) if compact(line).casefold() == "abstract"),
        min(15, len(lines)),
    )
    for index in range(1, abstract_index):
        for width in (1, 2, 3):
            block = " ".join(lines[index:index + width])
            entries = marked_author_entries(block)
            if len(entries) >= 2:
                return index, entries, compact(block)
    author_index, author_line = likely_author_line(lines)
    entries = [
        {"name": name, "marker": None, "equal_contribution": False}
        for name in split_author_segments(author_line)
    ]
    return author_index, entries, author_line


def marker_affiliations(first_page_text: str) -> dict[str, list[str]]:
    compacted = compact(first_page_text)
    footer_match = re.search(
        r"(?:\*Equal contribution|Equal contribution)(.+?)(?:Workshop on|Proceedings of|Copyright|\Z)",
        compacted,
        re.I,
    )
    if not footer_match:
        return {}
    footer = footer_match.group(1)
    results: dict[str, list[str]] = {}
    marker_pattern = re.compile(
        r"(?<!\d)(\d)\s*(?=[A-Z])(.+?)"
        r"(?=(?<!\d)\d\s*(?=[A-Z])|(?:Contact|Contract|Correspond|Communicat)\s+to\s*:|\Z)",
        re.I,
    )
    for match in marker_pattern.finditer(footer):
        marker = match.group(1)
        affiliation = compact(match.group(2)).strip(" .")
        if affiliation:
            results.setdefault(marker, []).append(affiliation)
    return results


def correspondence_bindings(text: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    pattern = re.compile(
        r"(?:Contact|Contract|Correspondence|Corresponding author)"
        r"\s*(?:to)?\s*:\s*"
        r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+){1,2})"
        r"\s*[<(]?\s*(" + EMAIL_RE.pattern + r")",
        re.I,
    )
    for match in pattern.finditer(text):
        bindings[compact(match.group(2)).casefold()] = compact(match.group(1))
    return bindings


def split_author_segments(author_line: str) -> list[str]:
    normalized = re.sub(r"\s+(?:and|&)\s+", ",", author_line, flags=re.I)
    segments = re.split(r"\s*[,;]\s*", normalized)
    result: list[str] = []
    for segment in segments:
        cleaned = re.sub(r"[*†‡\d]+$", "", segment).strip()
        cleaned = re.sub(r"^[\d*†‡\s]+", "", cleaned)
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’\-]+|[\u3400-\u9fff]{2,4}", cleaned)
        if 1 <= len(words) <= 6:
            name = compact(" ".join(words))
            if name.casefold() not in {"corresponding author", "authors"}:
                result.append(name)
    return unique(result)


def find_title(lines: list[str], author_index: int | None, fallback: str) -> str:
    upper = author_index if author_index is not None else min(5, len(lines))
    candidates = []
    for line in lines[:upper]:
        cleaned = compact(line)
        if (
            8 <= len(cleaned) <= 350
            and not EMAIL_RE.search(cleaned)
            and not DOI_RE.search(cleaned)
            and not any(word in cleaned.casefold() for word in INSTITUTION_WORDS)
        ):
            candidates.append(cleaned)
    return compact(" ".join(candidates[:3])) or fallback


def extract_directions(text: str, title: str) -> list[str]:
    keyword_match = re.search(
        r"(?:keywords?|关键词)\s*[:：]\s*([^\n\f]{3,500})", text, flags=re.I
    )
    if keyword_match:
        values = re.split(r"[,;；、|]", keyword_match.group(1))
        result = unique(values)
        if result:
            return result[:12]
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{3,}|[\u3400-\u9fff]{2,6}", title.casefold())
    counts = Counter(token for token in tokens if token not in STOPWORDS)
    return [word for word, _ in counts.most_common(10)]


def extract_funding(text: str) -> list[dict[str, Any]]:
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sentence in sentences:
        compacted = compact(sentence)
        if not compacted or not any(word in compacted.casefold() for word in FUNDING_WORDS):
            continue
        key = compacted.casefold()
        if key in seen:
            continue
        seen.add(key)
        project_ids = unique(PROJECT_RE.findall(compacted))
        results.append(
            {
                "statement": compacted[:1000],
                "project_ids": project_ids,
                "source": "pdf_text",
            }
        )
        if len(results) >= 12:
            break
    return results


def candidate_key(name: str, doi: str | None, source_pdf: str) -> str:
    raw = f"{name.casefold()}|{(doi or '').casefold()}|{source_pdf.casefold()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def parse_with_rules(path: Path, text: str, pages: list[str]) -> dict[str, Any]:
    lines = [compact(line) for line in text.splitlines() if compact(line)]
    author_index, author_entries, author_line = find_header_authors(lines)
    names = [entry["name"] for entry in author_entries]
    title = find_title(lines, author_index, path.stem)
    emails = unique(EMAIL_RE.findall(text))
    doi_match = DOI_RE.search(text)
    doi = doi_match.group(0).rstrip(".,;)").lower() if doi_match else None
    arxiv_match = ARXIV_RE.search(text)
    preprint_id = f"arXiv:{arxiv_match.group(1)}" if arxiv_match else None
    directions = extract_directions(text[:15000], title)
    institutions = unique(
        line[:500]
        for line in lines[:100]
        if any(word in line.casefold() for word in INSTITUTION_WORDS)
    )[:20]

    correspondence_text = " ".join(
        line for line in lines
        if re.search(r"correspond|communicat|contact to|contract to|通讯作者|通信作者", line, re.I)
    )
    bindings = correspondence_bindings(text[:15000])
    affiliations_by_marker = marker_affiliations(pages[0] if pages else text[:15000])
    surname_counts = Counter(re.split(r"\s+", name)[-1].casefold() for name in names)
    authors: list[dict[str, Any]] = []
    assigned_emails: set[str] = set()
    for index, entry in enumerate(author_entries, start=1):
        name = entry["name"]
        surname = re.split(r"\s+", name)[-1].casefold()
        matched_emails = []
        for email in emails:
            bound_name = bindings.get(email.casefold())
            if bound_name and normalized_person_name(bound_name) == normalized_person_name(name):
                matched_emails.append(email)
            elif (
                not bound_name
                and surname_counts[surname] == 1
                and surname in email.split("@", 1)[0].casefold()
            ):
                matched_emails.append(email)
        exact_corresponding = any(
            normalized_person_name(bound_name) == normalized_person_name(name)
            for bound_name in bindings.values()
        )
        is_corresponding = bool(
            exact_corresponding
            or (
                surname_counts[surname] == 1
                and re.search(re.escape(name), correspondence_text, re.I)
            )
        )
        assigned_emails.update(email.casefold() for email in matched_emails)
        author_institutions = affiliations_by_marker.get(compact(entry.get("marker")))
        if not author_institutions:
            author_institutions = institutions
        authors.append(
            {
                "name": name,
                "order": index,
                "is_corresponding": is_corresponding,
                "affiliation_marker": entry.get("marker"),
                "equal_contribution": bool(entry.get("equal_contribution")),
                "institutions": author_institutions,
                "paper_emails": matched_emails,
                "research_directions": directions,
                "field_confidence": {
                    "name": 0.92 if entry.get("marker") else 0.72,
                    "institutions": 0.82 if affiliations_by_marker else (0.45 if institutions else 0.0),
                    "paper_emails": 0.96 if matched_emails and exact_corresponding else (0.78 if matched_emails else 0.0),
                },
            }
        )

    journal = None
    journal_match = re.search(
        r"(?:journal|published in)\s*[:：]?\s*([^\n]{3,160})", text[:6000], re.I
    )
    if journal_match:
        journal = compact(journal_match.group(1))
    if not journal:
        venue_match = re.search(
            r"(Workshop on [^\n]{3,220}(?:\n[^\n]{3,180})?)",
            pages[0] if pages else text[:8000],
            re.I,
        )
        if venue_match:
            journal = compact(venue_match.group(1))
    warnings: list[str] = []
    if len(compact(text)) < 300:
        warnings.append("needs_ocr")
    if not authors:
        warnings.append("authors_not_confidently_extracted")
    if emails and not assigned_emails:
        warnings.append("emails_not_bound_to_specific_author")
    page_sources = [
        {"page": index, "text_chars": len(page)}
        for index, page in enumerate(pages, start=1)
    ]
    return {
        "source_pdf": str(path),
        "paper": {
            "title": title,
            "doi": doi,
            "preprint_id": preprint_id,
            "journal": journal,
            "publication_date": None,
            "research_directions": directions,
            "funding": extract_funding(text),
            "unassigned_emails": [
                email for email in emails if email.casefold() not in assigned_emails
            ],
        },
        "authors": authors,
        "quality": {
            "parser": "pypdf",
            "confidence": round(
                min(0.94, 0.25 + (0.25 if title else 0) + (0.3 if authors else 0)
                    + (0.14 if affiliations_by_marker else (0.1 if institutions else 0))),
                2,
            ),
            "warnings": warnings,
            "page_sources": page_sources,
        },
    }


def parse_pdf(path: Path, grobid_url: str | None, timeout: float) -> dict[str, Any]:
    warnings: list[str] = []
    if grobid_url:
        try:
            parsed = parse_grobid_tei(post_to_grobid(path, grobid_url, timeout), path)
            for author in parsed["authors"]:
                author["candidate_key"] = candidate_key(
                    author["name"], parsed["paper"].get("doi"), str(path)
                )
            return parsed
        except (HTTPError, URLError, TimeoutError, ET.ParseError, ValueError) as exc:
            warnings.append(f"grobid_fallback:{type(exc).__name__}")

    text, pages, _ = read_pdf_text(path)
    parsed = parse_with_rules(path, text, pages)
    parsed["quality"]["warnings"] = warnings + parsed["quality"]["warnings"]
    for author in parsed["authors"]:
        author["candidate_key"] = candidate_key(
            author["name"], parsed["paper"].get("doi"), str(path)
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Batch-extract paper and author metadata from academic PDFs."
    )
    parser.add_argument("inputs", nargs="+", help="PDF file(s) or directory/directories.")
    parser.add_argument("--output", required=True, help="Output UTF-8 JSON file.")
    parser.add_argument("--grobid-url", help="Optional approved GROBID service root URL.")
    parser.add_argument(
        "--language-hint",
        choices=("auto", "zh", "en"),
        default="auto",
        help="Reserved deterministic parser hint; never changes workflow decisions.",
    )
    parser.add_argument("--no-recursive", action="store_true", help="Do not recurse into directories.")
    parser.add_argument("--timeout", type=float, default=60.0, help="GROBID request timeout.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_path = Path(args.output).expanduser().resolve()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "language_hint": args.language_hint,
        "papers": [],
        "errors": [],
    }
    try:
        pdf_paths = collect_pdf_paths(args.inputs, recursive=not args.no_recursive)
    except (FileNotFoundError, PermissionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not pdf_paths:
        print("No PDF files found.", file=sys.stderr)
        return 2

    for path in pdf_paths:
        try:
            payload["papers"].append(parse_pdf(path, args.grobid_url, args.timeout))
        except Exception as exc:  # isolate individual files in a batch
            payload["errors"].append(
                {
                    "source_pdf": str(path),
                    "error": type(exc).__name__,
                    "message": compact(str(exc))[:500],
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "papers": len(payload["papers"]),
                "errors": len(payload["errors"]),
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["papers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

