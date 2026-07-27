from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from services.state_normalization import clean_text, term_matches

CONTEXT_REQUIRED_KEYWORDS = frozenset(
    {
        "claims",
        "eligibility",
        "enrollment",
        "workforce",
        "contact center",
        "provider data",
        "quality measures",
        "interoperability",
        "cms",
    }
)

HEALTH_CONTEXT_TERMS = (
    "medicaid",
    "medicare",
    "mmis",
    "chip",
    "mco",
    "managed care",
    "d-snp",
    "dsnp",
    "dual eligible",
    "ltss",
    "hcbs",
    "medical",
    "health",
    "healthcare",
    "health care",
    "health information",
    "health information exchange",
    "behavioral health",
    "rural health",
    "rural health transformation",
    "critical access hospital",
    "rural emergency hospital",
    "telehealth",
    "hospital",
    "human services",
    "health and human services",
    "department of health",
    "department of human services",
    "agency for health care administration",
    "ahca",
    "ahcccs",
    "tenncare",
    "hcpf",
    "health care policy",
    "health care policy and financing",
    "medi-cal",
    "masshealth",
    "vermont health access",
    "green mountain care",
    "wyoming department of health",
    "alabama medicaid",
    "rht",
)

CMS_CONTEXT_TERMS = (
    "centers for medicare",
    "centers for medicaid",
    "centers for medicare and medicaid",
    "centers for medicare & medicaid",
    "medicaid",
    "medicare",
    "state medicaid agency",
    "ahcccs",
    "tenncare",
    "hcpf",
    "health care policy and financing",
    "agency for health care administration",
    "medi-cal",
    "masshealth",
)


def useful_keyword_match(
    matches: list[str],
    text: Any,
    *,
    context_terms: Iterable[str] = (),
    cms_context_terms: Iterable[str] = (),
) -> bool:
    matched_terms = {_normalize_keyword(match) for match in matches if _normalize_keyword(match)}
    if not matched_terms:
        return False
    if not matched_terms <= CONTEXT_REQUIRED_KEYWORDS:
        return True

    if (matched_terms - {"cms"}) and _has_context(text, (*HEALTH_CONTEXT_TERMS, *context_terms)):
        return True
    if "cms" in matched_terms and _has_context(text, (*CMS_CONTEXT_TERMS, *cms_context_terms)):
        return True
    return False


def _normalize_keyword(value: Any) -> str:
    return clean_text(value, 100).lower()


def _has_context(text: Any, terms: Iterable[str]) -> bool:
    return any(term_matches(text, term) for term in terms)
