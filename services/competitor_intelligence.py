"""Local competitor identity resolution and analysis of already-collected records.

This module has no network or storage side effects.  Callers supply normalized contract
or opportunity mappings and receive copies enriched with identity metadata.
"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "competitor_aliases.csv"
ORGANIZATION_TYPES = frozenset({"gainwell", "competitor", "other"})

IDENTITY_FIELDS = (
    "vendor_name",
    "recipient_name",
    "incumbent_vendor_name",
    "awardee",
    "supplier_name",
    "contractor_name",
)
SEARCH_FIELDS = (
    *IDENTITY_FIELDS,
    "vendor_query",
    "vendor_key",
    "incumbent_vendor_key",
    "vendor_keys_mentioned",
    "organization_key",
    "organization_name",
    "title",
    "description",
    "summary",
    "agency",
    "subagency",
    "office",
    "matched_keywords",
    "topic_keys",
    "program_focus",
)
VALUE_FIELDS = (
    "current_total_value",
    "award_amount",
    "amount",
    "contract_value",
    "budget_estimate",
    "value",
)
END_DATE_FIELDS = ("period_end_date", "end_date", "due_date", "archive_date")
JURISDICTION_FIELDS = ("state", "jurisdiction", "place_of_performance_state")


@dataclass(frozen=True)
class Alias:
    text: str
    alias_type: str
    tokens: tuple[str, ...]
    row_order: int


@dataclass(frozen=True)
class OrganizationProfile:
    key: str
    canonical_name: str
    organization_type: str
    profile_order: int
    aliases: tuple[Alias, ...]


@dataclass(frozen=True)
class IdentityResolution:
    """Resolved identity. Unmatched input is retained in ``canonical_name``."""

    original_name: str
    organization_key: str
    canonical_name: str
    organization_type: str
    matched_alias: str = ""
    alias_type: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.organization_key)

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "original_name": self.original_name,
            "organization_key": self.organization_key,
            "canonical_name": self.canonical_name,
            "organization_type": self.organization_type,
            "matched_alias": self.matched_alias,
            "alias_type": self.alias_type,
            "matched": self.matched,
        }


def _tokens(value: Any) -> tuple[str, ...]:
    """Create punctuation-insensitive tokens while preserving word boundaries."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold().replace("&", " and ")
    parts = re.findall(r"[a-z0-9]+", text)
    # Procurement data often renders acronyms as C.N.S.I. or H M S.
    collapsed: list[str] = []
    index = 0
    while index < len(parts):
        if len(parts[index]) == 1 and parts[index].isalpha():
            end = index
            while end < len(parts) and len(parts[end]) == 1 and parts[end].isalpha():
                end += 1
            if end - index >= 2:
                collapsed.append("".join(parts[index:end]))
            else:
                collapsed.append(parts[index])
            index = end
        else:
            collapsed.append(parts[index])
            index += 1
    return tuple(collapsed)


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    if any(haystack[index:index + width] == needle for index in range(len(haystack) - width + 1)):
        return True
    # Dotted acronym plus dotted legal suffix may tokenize as one word (C.N.S.I., L.L.C.).
    if width == 1:
        for token in haystack:
            if token.startswith(needle[0]) and token[len(needle[0]):] in {"llc", "inc", "corp", "co", "lp", "llp"}:
                return True
    return False


def load_profiles(path: str | Path = DEFAULT_CONFIG_PATH) -> tuple[OrganizationProfile, ...]:
    """Load and validate the deterministic profile/alias configuration."""
    config_path = Path(path)
    required = {"profile_order", "organization_key", "canonical_name", "organization_type", "alias", "alias_type"}
    grouped: dict[str, dict[str, Any]] = {}
    alias_owners: dict[tuple[str, ...], str] = {}
    with config_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"invalid competitor alias config: missing columns in {config_path}")
        for row_order, row in enumerate(reader, start=2):
            key = (row.get("organization_key") or "").strip()
            canonical = (row.get("canonical_name") or "").strip()
            organization_type = (row.get("organization_type") or "").strip().lower()
            alias_text = (row.get("alias") or "").strip()
            alias_type = (row.get("alias_type") or "").strip()
            alias_tokens = _tokens(alias_text)
            if not key or not canonical or not alias_text or not alias_type or not alias_tokens:
                raise ValueError(f"invalid competitor alias row {row_order}: required value is blank")
            if organization_type not in ORGANIZATION_TYPES - {"other"}:
                raise ValueError(f"invalid organization_type on row {row_order}: {organization_type!r}")
            try:
                profile_order = int(row.get("profile_order") or 0)
            except ValueError as exc:
                raise ValueError(f"invalid profile_order on row {row_order}") from exc
            if profile_order < 1:
                raise ValueError(f"invalid profile_order on row {row_order}")
            owner = alias_owners.setdefault(alias_tokens, key)
            if owner != key:
                raise ValueError(f"competitor alias collision on row {row_order}: {alias_text!r}")
            group = grouped.setdefault(key, {
                "canonical_name": canonical,
                "organization_type": organization_type,
                "profile_order": profile_order,
                "aliases": [],
            })
            if (group["canonical_name"], group["organization_type"], group["profile_order"]) != (
                canonical, organization_type, profile_order
            ):
                raise ValueError(f"inconsistent profile values for {key!r}")
            group["aliases"].append(Alias(
                text=alias_text,
                alias_type=alias_type,
                tokens=alias_tokens,
                row_order=row_order,
            ))

    profiles = tuple(sorted((OrganizationProfile(
        key=key,
        canonical_name=values["canonical_name"],
        organization_type=values["organization_type"],
        profile_order=values["profile_order"],
        aliases=tuple(values["aliases"]),
    ) for key, values in grouped.items()), key=lambda profile: (profile.profile_order, profile.key)))
    if not profiles or profiles[0].key != "gainwell" or profiles[0].organization_type != "gainwell":
        raise ValueError("competitor alias config must place Gainwell first")
    if sum(profile.organization_type == "gainwell" for profile in profiles) != 1:
        raise ValueError("competitor alias config must contain exactly one Gainwell profile")
    return profiles


class CompetitorIntelligence:
    """Resolve identities and analyze normalized, already-collected records."""

    def __init__(self, profiles: Sequence[OrganizationProfile] | None = None) -> None:
        self.profiles = tuple(profiles) if profiles is not None else load_profiles()
        self._by_key = {profile.key.casefold(): profile for profile in self.profiles}
        candidates: list[tuple[OrganizationProfile, Alias]] = []
        for profile in self.profiles:
            candidates.extend((profile, alias) for alias in profile.aliases)
        # Most specific match wins; config order settles exact collisions.
        self._candidates = tuple(sorted(candidates, key=lambda item: (
            -len(item[1].tokens),
            -sum(len(token) for token in item[1].tokens),
            item[0].profile_order,
            item[1].row_order,
        )))

    def resolve(self, vendor_name: Any) -> IdentityResolution:
        original = str(vendor_name or "").strip()
        tokens = _tokens(original)
        for profile, alias in self._candidates:
            if _mention_alias_matches(tokens, alias, exact_identity=True):
                return IdentityResolution(
                    original_name=original,
                    organization_key=profile.key,
                    canonical_name=profile.canonical_name,
                    organization_type=profile.organization_type,
                    matched_alias=alias.text,
                    alias_type=alias.alias_type,
                )
        return IdentityResolution("" if vendor_name is None else original, "", original, "other")

    def classify(self, vendor_name: Any) -> str:
        return self.resolve(vendor_name).organization_type

    def find_mentions(self, record: Mapping[str, Any], fields: Sequence[str] = SEARCH_FIELDS) -> tuple[IdentityResolution, ...]:
        """Return each configured organization mentioned in a record, once."""
        found: dict[str, IdentityResolution] = {}
        normalized_key_fields = {"vendor_key", "incumbent_vendor_key", "vendor_keys_mentioned", "organization_key"}
        for field in fields:
            values = _field_values(record, (field,))
            for value in values:
                # Only explicit key fields may resolve directly by organization key.
                if field in normalized_key_fields:
                    for key in _split_keys(value):
                        profile = self._by_key.get(key.casefold())
                        if profile and profile.key not in found:
                            found[profile.key] = IdentityResolution(
                                str(value), profile.key, profile.canonical_name, profile.organization_type,
                                key, "normalized_key",
                            )
                value_tokens = _tokens(value)
                exact_identity = field in IDENTITY_FIELDS
                for profile, alias in self._candidates:
                    if profile.key not in found and _mention_alias_matches(value_tokens, alias, exact_identity):
                        found[profile.key] = IdentityResolution(
                            str(value), profile.key, profile.canonical_name,
                            profile.organization_type, alias.text, alias.alias_type,
                        )
        return tuple(found[profile.key] for profile in self.profiles if profile.key in found)

    def normalize_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Copy a record and attach primary identity plus all detected mentions."""
        result = dict(record)
        identity_value = next((record.get(field) for field in IDENTITY_FIELDS if str(record.get(field) or "").strip()), "")
        identity = self.resolve(identity_value)
        mentions = self.find_mentions(record)
        result["organization_key"] = identity.organization_key
        result["organization_name"] = identity.canonical_name
        result["organization_type"] = identity.organization_type
        result["matched_alias"] = identity.matched_alias
        result["organization_mentions"] = [mention.as_dict() for mention in mentions]
        return result

    def search_records(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        organization_keys: Iterable[str] | None = None,
        organization_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find configured organization mentions in collected records."""
        wanted_keys = {value.casefold() for value in organization_keys or ()}
        wanted_types = {value.casefold() for value in organization_types or ()}
        matches: list[dict[str, Any]] = []
        for record in records:
            mentions = self.find_mentions(record)
            if not mentions:
                continue
            if wanted_keys and not any(item.organization_key.casefold() in wanted_keys for item in mentions):
                continue
            if wanted_types and not any(item.organization_type in wanted_types for item in mentions):
                continue
            matches.append(self.normalize_record(record))
        return matches

    def custom_search(
        self,
        records: Iterable[Mapping[str, Any]],
        query: str,
        *,
        fields: Sequence[str] = SEARCH_FIELDS,
    ) -> list[dict[str, Any]]:
        """Case/punctuation-insensitive AND search over records already in memory."""
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        matches: list[dict[str, Any]] = []
        for record in records:
            record_tokens = _tokens(" ".join(str(value) for value in _field_values(record, fields)))
            if all(token in record_tokens for token in query_tokens):
                matches.append(self.normalize_record(record))
        return matches

    def summarize(self, records: Iterable[Mapping[str, Any]], *, as_of: dt.date | None = None) -> dict[str, Any]:
        return summarize_records(records, intelligence=self, as_of=as_of)


def resolve_organization(vendor_name: Any, *, profiles: Sequence[OrganizationProfile] | None = None) -> IdentityResolution:
    return CompetitorIntelligence(profiles).resolve(vendor_name)


def classify_organization(vendor_name: Any, *, profiles: Sequence[OrganizationProfile] | None = None) -> str:
    return CompetitorIntelligence(profiles).classify(vendor_name)


def search_mentions(
    records: Iterable[Mapping[str, Any]],
    *,
    organization_keys: Iterable[str] | None = None,
    organization_types: Iterable[str] | None = None,
    intelligence: CompetitorIntelligence | None = None,
) -> list[dict[str, Any]]:
    return (intelligence or CompetitorIntelligence()).search_records(
        records, organization_keys=organization_keys, organization_types=organization_types
    )


def custom_search(
    records: Iterable[Mapping[str, Any]],
    query: str,
    *,
    fields: Sequence[str] = SEARCH_FIELDS,
    intelligence: CompetitorIntelligence | None = None,
) -> list[dict[str, Any]]:
    return (intelligence or CompetitorIntelligence()).custom_search(records, query, fields=fields)


def summarize_records(
    records: Iterable[Mapping[str, Any]],
    *,
    intelligence: CompetitorIntelligence | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Any]:
    """Aggregate record counts/value, end windows, and jurisdictions."""
    engine = intelligence or CompetitorIntelligence()
    today = as_of or dt.date.today()
    summary: dict[str, Any] = {
        "record_count": 0,
        "total_value": 0.0,
        "by_organization": {},
        "by_organization_type": {},
        "by_jurisdiction": {},
        "end_windows": {
            "expired": 0,
            "0_90_days": 0,
            "91_180_days": 0,
            "181_365_days": 0,
            "over_365_days": 0,
            "unknown": 0,
        },
    }
    total = decimal.Decimal("0")
    for record in records:
        summary["record_count"] += 1
        value = _record_value(record)
        total += value
        jurisdiction = next((str(record.get(field) or "").strip() for field in JURISDICTION_FIELDS if str(record.get(field) or "").strip()), "Unknown")
        _add_bucket(summary["by_jurisdiction"], jurisdiction, value)

        mentions = engine.find_mentions(record)
        if not mentions:
            identity_value = next((record.get(field) for field in IDENTITY_FIELDS if str(record.get(field) or "").strip()), "")
            identity = engine.resolve(identity_value)
            mentions = (identity,)
        for mention in mentions:
            name = mention.canonical_name or "Unknown"
            _add_bucket(summary["by_organization"], name, value)
            _add_bucket(summary["by_organization_type"], mention.organization_type, value)

        end_date = next((_date(record.get(field)) for field in END_DATE_FIELDS if _date(record.get(field)) is not None), None)
        summary["end_windows"][_end_window(end_date, today)] += 1

    summary["total_value"] = float(total)
    return summary


def _field_values(record: Mapping[str, Any], fields: Sequence[str]) -> list[Any]:
    values: list[Any] = []
    for field in fields:
        value = record.get(field)
        if isinstance(value, (list, tuple, set)):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return values


def _split_keys(value: Any) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"[;,|]", str(value or "")) if part.strip())


def _mention_alias_matches(value_tokens: tuple[str, ...], alias: Alias, exact_identity: bool) -> bool:
    """Require short ambiguous acronyms to be exact vendor identities, never prose hits."""
    is_ambiguous_acronym = len(alias.tokens) == 1 and len(alias.tokens[0]) <= 3
    if not is_ambiguous_acronym:
        return _contains(value_tokens, alias.tokens)
    if not exact_identity:
        return False
    legal_suffixes = {"llc", "inc", "corp", "corporation", "company", "co", "lp", "llp"}
    return tuple(token for token in value_tokens if token not in legal_suffixes) == alias.tokens


def _record_value(record: Mapping[str, Any]) -> decimal.Decimal:
    for field in VALUE_FIELDS:
        raw = record.get(field)
        if raw not in (None, ""):
            cleaned = re.sub(r"[^0-9.()-]", "", str(raw))
            if cleaned.startswith("(") and cleaned.endswith(")"):
                cleaned = "-" + cleaned[1:-1]
            try:
                value = decimal.Decimal(cleaned or "0")
                return value if value.is_finite() else decimal.Decimal("0")
            except decimal.InvalidOperation:
                return decimal.Decimal("0")
    return decimal.Decimal("0")


def _date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _end_window(end_date: dt.date | None, today: dt.date) -> str:
    if end_date is None:
        return "unknown"
    days = (end_date - today).days
    if days < 0:
        return "expired"
    if days <= 90:
        return "0_90_days"
    if days <= 180:
        return "91_180_days"
    if days <= 365:
        return "181_365_days"
    return "over_365_days"


def _add_bucket(buckets: dict[str, dict[str, Any]], key: str, value: decimal.Decimal) -> None:
    bucket = buckets.setdefault(key, {"count": 0, "value": 0.0})
    bucket["count"] += 1
    bucket["value"] = float(decimal.Decimal(str(bucket["value"])) + value)
