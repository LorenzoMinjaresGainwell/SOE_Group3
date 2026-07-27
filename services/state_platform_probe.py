from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

PLATFORM_FAMILIES = (
    "bso_periscope_buyspeed",
    "cgi_advantage_vss",
    "peoplesoft_supplier",
    "jaggaer_sciquest",
    "socrata_open_data",
    "bonfire",
    "html_form_search_table",
    "custom_state_portal",
    "manual_probe_required",
)

STRONG_SIGNAL_RULES = (
    (
        "bso_periscope_buyspeed",
        "bso_periscope_buyspeed_signal",
        (
            r"/bso(?:/|\b)",
            r"\bbuyspeed\b",
            r"\bperiscope\b",
            r"\bbidbuy\b",
            r"\bcommbuys\b",
            r"\bnjstart\b",
            r"\bnevadaepro\b",
            r"\boregonbuys\b",
            r"javax\.faces\.ViewState",
            r"\bJSF\b",
        ),
    ),
    (
        "cgi_advantage_vss",
        "cgi_advantage_vss_signal",
        (
            r"AltSelfService",
            r"/webapp/PRDVSS",
            r"\bCGI Advantage\b",
            r"\bCGI Federal\b",
            r"\bVendor Self Service\b",
            r"\bVSS\b",
            r"\bwvOASIS\b",
            r"\bSIGMA\b",
            r"\beMARS\b",
        ),
    ),
    (
        "peoplesoft_supplier",
        "peoplesoft_supplier_signal",
        (
            r"/psc/",
            r"/psp/",
            r"\bPeopleSoft\b",
            r"PT_LANDINGPAGE",
            r"ICAction=",
            r"SUPPLIER/ERP",
            r"\bSWIFT Supplier\b",
            r"\bEdison Supplier\b",
        ),
    ),
    (
        "jaggaer_sciquest",
        "jaggaer_sciquest_signal",
        (
            r"apps/Router/PublicEvent",
            r"\bSciQuest\b",
            r"\bJAGGAER\b",
            r"CustomerOrg=",
            r"bids\.sciquest\.com",
            r"bravosolution",
        ),
    ),
    (
        "socrata_open_data",
        "socrata_open_data_signal",
        (
            r"\bSocrata\b",
            r"\bSODA\b",
            r"/api/views/",
            r"/resource/[a-z0-9]{4}-[a-z0-9]{4}",
            r"data\.[A-Za-z0-9.-]+/(?:api|resource)",
            r"\bopen data\b",
        ),
    ),
    (
        "bonfire",
        "bonfire_signal",
        (
            r"\bBonfire\b",
            r"bonfirehub\.com",
            r"gobonfire\.com",
            r"portal\.bonfire",
        ),
    ),
)

HTML_SIGNAL_RULES = (
    ("html_form", r"<form\b"),
    ("html_table", r"<table\b"),
    ("aspnet_viewstate", r"__VIEWSTATE"),
    ("aspx_page", r"\.aspx(?:\?|\b)"),
)

SOURCE_TEXT_FIELDS = (
    "source_key",
    "source_name",
    "target_url",
    "target_api",
    "access_method",
    "platform",
    "notes",
    "raw_platform",
    "raw_access_method",
    "group_notes",
)


@dataclass(frozen=True)
class ProbeResult:
    platform_family: str
    confidence: str
    evidence: list[str]
    matched_signals: list[str]
    input_url: str = ""
    final_url: str = ""
    content_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_family": self.platform_family,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "matched_signals": self.matched_signals,
            "input_url": self.input_url,
            "final_url": self.final_url,
            "content_type": self.content_type,
        }


def classify_source(source: dict[str, Any], *, html: str = "", final_url: str = "", content_type: str = "") -> ProbeResult:
    return classify_platform_evidence(
        url=str(source.get("target_url") or ""),
        html=html,
        final_url=final_url,
        content_type=content_type,
        source=source,
    )


def classify_platform_evidence(
    *,
    url: str = "",
    html: str = "",
    final_url: str = "",
    content_type: str = "",
    source: dict[str, Any] | None = None,
) -> ProbeResult:
    source = source or {}
    evidence_text = "\n".join([url, final_url, content_type, source_evidence_text(source), html[:200_000]])
    evidence: list[str] = []
    matched_signals: list[str] = []

    for family, signal_name, patterns in STRONG_SIGNAL_RULES:
        hits = matching_patterns(evidence_text, patterns)
        if hits:
            evidence.extend(hits[:5])
            matched_signals.append(signal_name)
            return ProbeResult(
                platform_family=family,
                confidence="high",
                evidence=dedupe(evidence),
                matched_signals=dedupe(matched_signals),
                input_url=url,
                final_url=final_url,
                content_type=content_type,
            )

    access_method = str(source.get("access_method") or source.get("raw_access_method") or "")
    if access_method == "manual_probe_required":
        evidence.append("source catalog access_method=manual_probe_required")
        return build_result("manual_probe_required", "medium", evidence, matched_signals, url, final_url, content_type)

    if is_pdf_or_static_document(url=url, final_url=final_url, content_type=content_type):
        evidence.append("target appears to be a static document/PDF")
        return build_result("manual_probe_required", "medium", evidence, matched_signals, url, final_url, content_type)

    html_hits = html_signal_hits(evidence_text)
    if access_method in {"html_form_adapter", "html_search_adapter", "html_table_adapter", "html_page_adapter"}:
        evidence.append(f"source catalog access_method={access_method}")
        evidence.extend(html_hits)
        return build_result("html_form_search_table", "medium", evidence, matched_signals, url, final_url, content_type)
    if html_hits:
        evidence.extend(html_hits)
        return build_result("html_form_search_table", "medium", evidence, matched_signals, url, final_url, content_type)

    if access_method == "official_portal_json" or "json" in content_type.lower():
        evidence.append("official or JSON-backed public portal evidence")
        return build_result("custom_state_portal", "medium", evidence, matched_signals, url, final_url, content_type)

    if access_method in {"public_portal_adapter", "vendor_platform_adapter"}:
        evidence.append(f"source catalog access_method={access_method}; no known shared platform signal")
        return build_result("custom_state_portal", "low", evidence, matched_signals, url, final_url, content_type)

    if looks_like_html(content_type, html):
        evidence.append("HTML page without known form/table/platform signal")
        return build_result("custom_state_portal", "low", evidence, matched_signals, url, final_url, content_type)

    evidence.append("insufficient evidence for platform classification")
    return build_result("manual_probe_required", "low", evidence, matched_signals, url, final_url, content_type)


def build_result(
    family: str,
    confidence: str,
    evidence: list[str],
    matched_signals: list[str],
    url: str,
    final_url: str,
    content_type: str,
) -> ProbeResult:
    if family not in PLATFORM_FAMILIES:
        family = "manual_probe_required"
        confidence = "low"
    return ProbeResult(
        platform_family=family,
        confidence=confidence,
        evidence=dedupe(evidence),
        matched_signals=dedupe(matched_signals),
        input_url=url,
        final_url=final_url,
        content_type=content_type,
    )


def source_evidence_text(source: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in SOURCE_TEXT_FIELDS:
        value = source.get(field)
        if value:
            parts.append(str(value))
    for field in ("information_tags", "adapter_targets"):
        for value in source.get(field) or []:
            parts.append(str(value))
    return "\n".join(parts)


def matching_patterns(text: str, patterns: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(pattern)
    return hits


def html_signal_hits(text: str) -> list[str]:
    hits: list[str] = []
    for name, pattern in HTML_SIGNAL_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            hits.append(name)
    return hits


def is_pdf_or_static_document(*, url: str, final_url: str, content_type: str) -> bool:
    lower_url = " ".join([url, final_url]).lower()
    lower_type = content_type.lower()
    return (
        "application/pdf" in lower_type
        or lower_url.endswith(".pdf")
        or any(lower_url.endswith(ext) for ext in (".doc", ".docx", ".xls", ".xlsx", ".zip"))
    )


def looks_like_html(content_type: str, html: str) -> bool:
    lower_type = content_type.lower()
    lower_html = html[:1000].lower()
    return "text/html" in lower_type or "<html" in lower_html or "<!doctype html" in lower_html


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
