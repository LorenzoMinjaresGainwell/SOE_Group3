from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from services.state_http import fetch_url

USER_AGENT = "Mozilla/5.0 (compatible; soe-group3-state-updates/0.1)"
DATE_RE = re.compile(
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b"
    r"|\b\d{1,2}/\d{1,2}/\d{4}\b"
    r"|\b\d{1,2}-\d{1,2}-\d{4}\b"
    r"|\b20\d{2}-\d{2}-\d{2}\b",
    re.I,
)
PROCUREMENT_RE = re.compile(
    r"\b(?:rfp|rfq|itb|solicitations?|bids?|awards?|procurements?|contracts?|re[- ]?competes?)\b",
    re.I,
)


@dataclass
class HtmlLink:
    text: str
    href: str


@dataclass
class HtmlCell:
    text: str = ""
    links: list[HtmlLink] = field(default_factory=list)


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[HtmlCell]]] = []
        self._table_depth = 0
        self._row: list[HtmlCell] | None = None
        self._cell: HtmlCell | None = None
        self._cell_parts: list[str] = []
        self._link_href = ""
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self.tables.append([])
        elif tag == "tr" and self._table_depth == 1:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = HtmlCell()
            self._cell_parts = []
        elif tag == "a" and self._cell is not None:
            self._link_href = attrs_dict.get("href", "")
            self._link_parts = []
        elif tag == "br" and self._cell is not None:
            self._cell_parts.append(" ")
            if self._link_href:
                self._link_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._cell is not None and self._link_href:
            text = clean_text(" ".join(self._link_parts))
            self._cell.links.append(HtmlLink(text=text, href=self._link_href))
            self._link_href = ""
            self._link_parts = []
        elif tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._cell.text = clean_text(" ".join(self._cell_parts))
            self._row.append(self._cell)
            self._cell = None
            self._cell_parts = []
        elif tag == "tr" and self._table_depth == 1 and self._row is not None:
            if any(cell.text or cell.links for cell in self._row):
                self.tables[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell is None:
            return
        self._cell_parts.append(data)
        if self._link_href:
            self._link_parts.append(data)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[HtmlLink] = []
        self._href = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {key.lower(): value or "" for key, value in attrs}
        self._href = attrs_dict.get("href", "")
        self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append(HtmlLink(text=clean_text(" ".join(self._parts)), href=self._href))
            self._href = ""
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._parts.append(data)


def fetch_text(url: str, *, timeout: int = 30, byte_limit: int = 3_000_000) -> str:
    result = fetch_url(
        url,
        headers={"Accept": "text/html,application/xhtml+xml,application/json,*/*"},
        timeout=timeout,
        byte_limit=byte_limit,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return result.body_text()


def fetch_json_data(url: str, *, timeout: int = 30, byte_limit: int = 3_000_000) -> Any:
    result = fetch_url(
        url,
        headers={"Accept": "application/json,text/javascript,*/*", "Referer": referer_for(url)},
        timeout=timeout,
        byte_limit=byte_limit,
        user_agent=USER_AGENT,
    )
    result.raise_for_status()
    return result.json_data()


def parse_tables(markup: str) -> list[list[list[HtmlCell]]]:
    parser = TableParser()
    parser.feed(markup)
    return parser.tables


def parse_links(markup: str, base_url: str) -> list[HtmlLink]:
    parser = LinkParser()
    parser.feed(markup)
    return [HtmlLink(text=link.text, href=absolute_url(base_url, link.href)) for link in parser.links if link.href]


def find_table(tables: list[list[list[HtmlCell]]], required_headers: list[str]) -> list[list[HtmlCell]]:
    required = [header.lower() for header in required_headers]
    for table in tables:
        for row in table[:3]:
            header_text = " | ".join(cell.text.lower() for cell in row)
            if all(header in header_text for header in required):
                return table
    return []


def data_rows(table: list[list[HtmlCell]]) -> list[list[HtmlCell]]:
    rows = []
    for row in table:
        text = " | ".join(cell.text.lower() for cell in row)
        if any(header in text for header in ("issue date", "release date", "notice date", "division/office")):
            continue
        rows.append(row)
    return rows


def absolute_url(base_url: str, href: str) -> str:
    value = html.unescape(str(href or "")).strip()
    if not value:
        return ""
    if value.startswith("www."):
        value = "https://" + value
    joined = urllib.parse.urljoin(base_url, value)
    parts = urllib.parse.urlsplit(joined)
    path = urllib.parse.quote(parts.path, safe="/%")
    query = urllib.parse.quote(parts.query, safe="=&%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def public_hfs_url(url: str) -> str:
    return url.replace("https://hfs.illinois.gov/content/soi/hfs/en", "https://hfs.illinois.gov")


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_query(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def source_id_from_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path_id = clean_text(urllib.parse.unquote(parts.path).strip("/")).replace("/", ":")
    if not parts.query:
        return path_id[:240]
    # Query parameters frequently carry the only item identity (for example,
    # ``download?id=123``). Hash them rather than dropping them or persisting
    # potentially sensitive parameter values.
    query_hash = hashlib.sha256(parts.query.encode("utf-8")).hexdigest()[:16]
    return f"{path_id[:217]}:query-{query_hash}".lstrip(":")


def is_procurement_update(*values: str) -> bool:
    """Match procurement language in human text and percent-decoded URLs."""
    text = " ".join(urllib.parse.unquote(str(value or "")) for value in values)
    return bool(PROCUREMENT_RE.search(text))


def title_from_url(url: str) -> str:
    name = urllib.parse.urlsplit(strip_query(url)).path.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.(shtml|html|pdf|docx?)$", "", name, flags=re.I)
    name = re.sub(r"[_-]+", " ", name)
    return clean_text(name).title()


def first_date_text(value: str) -> str:
    match = DATE_RE.search(clean_text(value))
    return match.group(0) if match else ""


def iso_date_text(value: str) -> str:
    text = first_date_text(value) or clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\b([A-Za-z]+)", lambda m: m.group(1).capitalize(), text)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def iso_http_date(value: str) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""
    return parsed.date().isoformat() if parsed else ""


def head_last_modified(url: str, *, timeout: int = 15) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return iso_http_date(response.headers.get("Last-Modified", ""))
    except OSError:
        return ""


def meta_modified(markup: str) -> str:
    match = re.search(r"<meta\s+[^>]*name=[\"']modified[\"'][^>]*content=[\"']([^\"']+)[\"']", markup, re.I)
    return iso_date_text(match.group(1)) if match else ""


def due_date_from_text(value: str, posted_date: str = "") -> str:
    text = clean_text(value)
    exact = iso_date_text(text)
    if exact:
        return exact
    if posted_date and re.search(r"30\s+days\s+after\s+posted", text, re.I):
        try:
            return (dt.date.fromisoformat(posted_date) + dt.timedelta(days=30)).isoformat()
        except ValueError:
            return ""
    return ""


def record_type_for(title: str, default: str = "policy_update") -> str:
    lower = title.lower()
    if "rural health transformation" in lower or "rht" in lower:
        return "rht_notice"
    if "grant" in lower or "applications" in lower or "fund" in lower:
        return "grant_notice"
    if "state plan amendment" in lower or re.search(r"\bspa\b", lower):
        return "spa_notice"
    if "waiver" in lower or "1115" in lower:
        return "waiver_notice"
    if "public notice" in lower or "comment" in lower:
        return "public_comment_notice"
    if "bulletin" in lower:
        return "provider_bulletin"
    return default


def matches_keywords_or_context(text: str, keywords: list[str], context_terms: list[str]) -> bool:
    lower = clean_text(text).lower()
    if any(str(keyword).strip().lower() in lower for keyword in keywords if str(keyword).strip()):
        return True
    return any(term.lower() in lower for term in context_terms)


def unique_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for record in records:
        key = record.get("id") or "|".join([record.get("state", ""), record.get("source", ""), record.get("source_record_id", "")])
        if key in seen:
            continue
        seen.add(key)
        output.append(record)
    return output


def referer_for(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
