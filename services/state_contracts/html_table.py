from __future__ import annotations

import urllib.parse
from html.parser import HTMLParser
from typing import Any

from services.state_normalization import clean_text


class HtmlTableParser(HTMLParser):
    """Small, dependency-free parser for ordinary official HTML tables."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.in_cell = False
        self.parts: list[str] = []
        self.links: list[str] = []
        self.row: list[dict[str, Any]] | None = None
        self.rows: list[list[dict[str, Any]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.in_cell = True
            self.parts = []
            self.links = []
        elif self.in_cell and tag in {"br", "li", "p", "div"}:
            self.parts.append(" | ")
        elif self.in_cell and tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(urllib.parse.urljoin(self.base_url, href))

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_cell and self.row is not None:
            self.row.append({"text": clean_text(" ".join(self.parts), 3000).strip(" |"), "links": self.links[:]})
            self.in_cell = False
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def parse_table_rows(markup: str, base_url: str) -> list[list[dict[str, Any]]]:
    parser = HtmlTableParser(base_url)
    parser.feed(markup)
    return parser.rows
