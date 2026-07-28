from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PARAMETERS_PATH = ROOT / "data" / "search_parameters.json"
DEFAULT_CAPABILITY_RULES_PATH = ROOT / "data" / "capability_rules.csv"
DEFAULT_COMPETITOR_ALIASES_PATH = ROOT / "data" / "competitor_aliases.csv"

_REQUIRED_GROUPS = (
    "rht_explicit",
    "rht_direct",
    "rht_related",
    "health_programs",
    "gainwell_capabilities",
    "negative_noise",
)

# Used only when an older search_parameters.json has no taxonomy section.
_LEGACY_GROUPS: dict[str, tuple[str, ...]] = {
    "rht_explicit": ("rural health transformation", "rhtp", "rht"),
    "rht_direct": ("rural health", "rural hospital", "critical access hospital"),
    "rht_related": ("telehealth", "behavioral health", "workforce"),
    "health_programs": ("Medicaid", "Medicare", "CMS", "CHIP", "MMIS"),
    "gainwell_capabilities": (
        "claims", "eligibility", "enrollment", "managed care", "interoperability",
        "FHIR", "prior authorization", "contact center", "provider data", "quality measures",
    ),
    "negative_noise": ("job", "jobs", "career", "careers", "training", "webinar"),
}


@dataclass(frozen=True)
class SearchTaxonomy:
    groups: Mapping[str, tuple[str, ...]]
    monitored_terms: tuple[str, ...]
    aliases_by_organization: Mapping[str, tuple[str, ...]]
    canonical_names: Mapping[str, str]
    raw_parameters: Mapping[str, Any]

    def terms(self, *groups: str) -> list[str]:
        unknown = [name for name in groups if name not in self.groups]
        if unknown:
            raise KeyError(f"unknown taxonomy group(s): {', '.join(unknown)}")
        return ordered_dedupe(term for name in groups for term in self.groups[name])

    @property
    def rht_terms(self) -> list[str]:
        return self.terms("rht_explicit", "rht_direct", "rht_related")

    @property
    def business_terms(self) -> list[str]:
        return list(self.monitored_terms)

    @property
    def competitor_aliases(self) -> list[str]:
        return ordered_dedupe(
            alias
            for key, aliases in self.aliases_by_organization.items()
            if key != "gainwell"
            for alias in aliases
        )


def ordered_dedupe(values: Iterable[Any]) -> list[str]:
    """Strip values and dedupe case-insensitively without changing first-seen order."""
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def boundary_pattern(term: str) -> re.Pattern[str]:
    """Match a configured term as words/tokens, not as part of another word."""
    cleaned = str(term).strip()
    if not cleaned:
        raise ValueError("search term cannot be blank")
    escaped = re.escape(cleaned).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


def contains_term(text: Any, term: str) -> bool:
    return bool(boundary_pattern(term).search(str(text or "")))


def matching_terms(text: Any, terms: Iterable[str]) -> list[str]:
    value = str(text or "")
    return [term for term in ordered_dedupe(terms) if contains_term(value, term)]


def load_search_parameters(path: str | Path = DEFAULT_PARAMETERS_PATH) -> dict[str, Any]:
    """Read the legacy-compatible JSON object, with validation instead of silent coercion."""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid search parameter JSON in {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"search parameters must be a JSON object: {config_path}")
    return payload


def load_search_taxonomy(
    parameters_path: str | Path = DEFAULT_PARAMETERS_PATH,
    *,
    capability_rules_path: str | Path = DEFAULT_CAPABILITY_RULES_PATH,
    competitor_aliases_path: str | Path = DEFAULT_COMPETITOR_ALIASES_PATH,
) -> SearchTaxonomy:
    params = load_search_parameters(parameters_path)
    configured = params.get("taxonomy") or {}
    if not isinstance(configured, dict):
        raise ValueError("search_parameters taxonomy must be an object")

    groups: dict[str, tuple[str, ...]] = {}
    for name in _REQUIRED_GROUPS:
        values = configured.get(name, _LEGACY_GROUPS[name])
        if not isinstance(values, (list, tuple)) or not all(isinstance(value, str) for value in values):
            raise ValueError(f"taxonomy.{name} must be a list of strings")
        cleaned = tuple(ordered_dedupe(values))
        if not cleaned:
            raise ValueError(f"taxonomy.{name} cannot be empty")
        groups[name] = cleaned

    rule_terms = _load_rule_terms(Path(capability_rules_path))
    for tier, group_name in (("explicit", "rht_explicit"), ("direct", "rht_direct"), ("related", "rht_related")):
        groups[group_name] = tuple(ordered_dedupe([*groups[group_name], *rule_terms.get(f"rht:{tier}", ())]))
    groups["gainwell_capabilities"] = tuple(ordered_dedupe(
        [*groups["gainwell_capabilities"], *rule_terms.get("capability", ())]
    ))

    aliases, canonical_names = _load_aliases(Path(competitor_aliases_path), params)
    legacy_monitored = params.get("monitored_keywords")
    if legacy_monitored is not None:
        if not isinstance(legacy_monitored, list) or not all(isinstance(value, str) for value in legacy_monitored):
            raise ValueError("monitored_keywords must be a list of strings")
        monitored = ordered_dedupe(legacy_monitored)
    else:
        monitored = ordered_dedupe(
            term for name in _REQUIRED_GROUPS if name != "negative_noise" for term in groups[name]
        )
    if not monitored:
        raise ValueError("monitored business terms cannot be empty")

    return SearchTaxonomy(
        groups=MappingProxyType(groups),
        monitored_terms=tuple(monitored),
        aliases_by_organization=MappingProxyType(aliases),
        canonical_names=MappingProxyType(canonical_names),
        raw_parameters=MappingProxyType(params),
    )


def _load_rule_terms(path: Path) -> dict[str, tuple[str, ...]]:
    if not path.exists():
        return {}
    grouped: dict[str, list[str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"category", "tier", "terms"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"invalid capability rules config: missing columns in {path}")
        for row_number, row in enumerate(reader, start=2):
            category = (row.get("category") or "").strip().lower()
            tier = (row.get("tier") or "").strip().lower()
            values = (row.get("terms") or "").split("|")
            if category == "capability":
                key = "capability"
            elif category == "rht" and tier in {"explicit", "direct", "related", "generic"}:
                key = f"rht:{tier}"
            else:
                raise ValueError(f"invalid capability category/tier on row {row_number}: {category!r}/{tier!r}")
            grouped.setdefault(key, []).extend(values)
    return {key: tuple(ordered_dedupe(values)) for key, values in grouped.items()}


def _load_aliases(path: Path, params: Mapping[str, Any]) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    grouped: dict[str, list[str]] = {}
    canonical: dict[str, str] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"organization_key", "canonical_name", "organization_type", "alias"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"invalid competitor alias config: missing columns in {path}")
            for row_number, row in enumerate(reader, start=2):
                key = (row.get("organization_key") or "").strip()
                name = (row.get("canonical_name") or "").strip()
                alias = (row.get("alias") or "").strip()
                if not key or not name or not alias:
                    raise ValueError(f"invalid competitor alias row {row_number}: required value is blank")
                if key in canonical and canonical[key] != name:
                    raise ValueError(f"inconsistent canonical name for {key!r}")
                canonical[key] = name
                grouped.setdefault(key, []).append(alias)

    # Older/custom configurations remain valid and can add aliases.
    vendors = params.get("vendors") or []
    if not isinstance(vendors, list):
        raise ValueError("vendors must be a list")
    for item in vendors:
        if isinstance(item, str):
            name, item_aliases = item.strip(), []
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            raw_aliases = item.get("aliases") or []
            if not isinstance(raw_aliases, list):
                raise ValueError(f"aliases for vendor {name!r} must be a list")
            item_aliases = [str(alias) for alias in raw_aliases]
        else:
            raise ValueError("each vendor must be a string or object")
        if not name:
            continue
        key = _organization_key(name, canonical)
        canonical.setdefault(key, name)
        grouped.setdefault(key, []).extend([name, *item_aliases])

    aliases = {key: tuple(ordered_dedupe(values)) for key, values in grouped.items()}
    return aliases, canonical


def _organization_key(name: str, canonical: Mapping[str, str]) -> str:
    folded = name.casefold()
    for key, value in canonical.items():
        if value.casefold() == folded:
            return key
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", folded)).strip("_")
