from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS_PATH = ROOT / "data" / "state_api_targets.json"
DEFAULT_GROUPS_PATH = ROOT / "data" / "state_platform_groups.json"

REQUIRED_SOURCE_FIELDS = {
    "source_key",
    "source_name",
    "target_url",
    "target_api",
    "access_method",
    "platform",
    "requires_api_key",
    "information_tags",
    "adapter_targets",
    "confidence",
}

PRIORITY_RANK = {
    "implemented": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


class CatalogError(ValueError):
    """Raised when state source catalog files are missing or inconsistent."""


class StateSourceCatalog:
    def __init__(self, target_catalog: dict[str, Any], platform_groups: dict[str, Any]) -> None:
        self.target_catalog = target_catalog
        self.platform_groups = platform_groups
        self._group_index = build_group_index(platform_groups)
        self._sources = flatten_sources(target_catalog, self._group_index)
        self._source_by_key = {source["source_key"]: source for source in self._sources}

    @classmethod
    def load(
        cls,
        *,
        targets_path: Path | str = DEFAULT_TARGETS_PATH,
        groups_path: Path | str = DEFAULT_GROUPS_PATH,
        validate: bool = True,
    ) -> "StateSourceCatalog":
        target_catalog = load_json(Path(targets_path))
        platform_groups = load_json(Path(groups_path))
        errors = validate_catalogs(target_catalog, platform_groups) if validate else []
        if errors:
            raise CatalogError("State source catalog validation failed:\n" + "\n".join(f"- {error}" for error in errors))
        return cls(target_catalog, platform_groups)

    def all_sources(self) -> list[dict[str, Any]]:
        return list(self._sources)

    def get_source(self, source_key: str) -> dict[str, Any] | None:
        return self._source_by_key.get(source_key)

    def require_source(self, source_key: str) -> dict[str, Any]:
        source = self.get_source(source_key)
        if source is None:
            raise KeyError(f"unknown source_key: {source_key}")
        return source

    def query_sources(
        self,
        *,
        source_key: str | None = None,
        state: str | list[str] | None = None,
        platform_family: str | list[str] | None = None,
        tags: str | list[str] | None = None,
        adapter_targets: str | list[str] | None = None,
        priority: str | list[str] | None = None,
        confidence: str | list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        states = normalize_filter_values(state)
        families = normalize_filter_values(platform_family)
        tag_values = normalize_filter_values(tags)
        target_values = normalize_filter_values(adapter_targets)
        priorities = normalize_filter_values(priority)
        confidences = normalize_filter_values(confidence)

        matches: list[dict[str, Any]] = []
        for source in self._sources:
            if source_key and source.get("source_key") != source_key:
                continue
            if states and not any(source_matches_state(source, value) for value in states):
                continue
            if families and normalize_value(source.get("platform_family")) not in families:
                continue
            if priorities and normalize_value(source.get("implementation_priority")) not in priorities:
                continue
            if confidences and not confidence_matches(source, confidences):
                continue
            if tag_values and not contains_all(source.get("information_tags") or [], tag_values):
                continue
            if target_values and not contains_all(source.get("adapter_targets") or [], target_values):
                continue
            matches.append(source)
            if limit is not None and len(matches) >= limit:
                break
        return matches

    def platform_families(self) -> list[str]:
        families = {str(group.get("platform_family") or "") for group in self.platform_groups.get("groups") or []}
        return sorted(family for family in families if family)

    def states(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for jurisdiction in self.target_catalog.get("jurisdictions") or []:
            rows.append(
                {
                    "code": str(jurisdiction.get("code") or ""),
                    "name": str(jurisdiction.get("name") or ""),
                    "jurisdiction_type": str(jurisdiction.get("jurisdiction_type") or ""),
                    "implementation_priority": str(jurisdiction.get("implementation_priority") or ""),
                    "adapter_status": str(jurisdiction.get("adapter_status") or ""),
                }
            )
        return rows

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.target_catalog.get("schema_version"),
            "source_count": len(self._sources),
            "jurisdiction_count": len(self.target_catalog.get("jurisdictions") or []),
            "platform_family_counts": count_by(self._sources, "platform_family"),
            "priority_counts": count_by(self._sources, "implementation_priority"),
            "adapter_target_counts": count_members(self._sources, "adapter_targets"),
            "tag_counts": count_members(self._sources, "information_tags"),
        }


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise CatalogError(f"catalog file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"catalog root must be an object: {path}")
    return data


def validate_catalogs(target_catalog: dict[str, Any], platform_groups: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source_keys: set[str] = set()

    jurisdictions = target_catalog.get("jurisdictions")
    if not isinstance(jurisdictions, list) or not jurisdictions:
        errors.append("data/state_api_targets.json must contain a non-empty jurisdictions list")
        jurisdictions = []

    for index, jurisdiction in enumerate(jurisdictions):
        if not isinstance(jurisdiction, dict):
            errors.append(f"jurisdictions[{index}] must be an object")
            continue
        code = str(jurisdiction.get("code") or "").strip().upper()
        if not code:
            errors.append(f"jurisdictions[{index}] missing code")
        sources = jurisdiction.get("sources")
        if not isinstance(sources, list) or not sources:
            errors.append(f"jurisdiction {code or index} must contain a non-empty sources list")
            continue
        for source_index, source in enumerate(sources):
            prefix = f"jurisdiction {code or index} sources[{source_index}]"
            if not isinstance(source, dict):
                errors.append(f"{prefix} must be an object")
                continue
            missing = sorted(field for field in REQUIRED_SOURCE_FIELDS if field not in source)
            if missing:
                errors.append(f"{prefix} missing fields: {', '.join(missing)}")
            source_key = str(source.get("source_key") or "").strip()
            if not source_key:
                errors.append(f"{prefix} missing source_key")
            elif source_key in source_keys:
                errors.append(f"duplicate source_key: {source_key}")
            else:
                source_keys.add(source_key)
            for list_field in ("information_tags", "adapter_targets"):
                if not isinstance(source.get(list_field), list):
                    errors.append(f"{prefix}.{list_field} must be a list")
            if source.get("requires_api_key") is not False:
                errors.append(f"{prefix}.requires_api_key must be false for MVP public-source catalogs")

    groups = platform_groups.get("groups")
    if not isinstance(groups, list) or not groups:
        errors.append("data/state_platform_groups.json must contain a non-empty groups list")
        groups = []

    seen_group_sources: set[str] = set()
    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            errors.append(f"groups[{group_index}] must be an object")
            continue
        family = str(group.get("platform_family") or "").strip()
        if not family:
            errors.append(f"groups[{group_index}] missing platform_family")
        group_sources = group.get("sources")
        if not isinstance(group_sources, list):
            errors.append(f"group {family or group_index} sources must be a list")
            continue
        for source in group_sources:
            source_key = str((source or {}).get("source_key") or "").strip() if isinstance(source, dict) else ""
            if not source_key:
                errors.append(f"group {family or group_index} has source without source_key")
                continue
            if source_key not in source_keys:
                errors.append(f"group {family or group_index} references unknown source_key: {source_key}")
            if source_key in seen_group_sources:
                errors.append(f"source_key appears in multiple platform groups: {source_key}")
            seen_group_sources.add(source_key)

    missing_group_refs = sorted(source_keys - seen_group_sources)
    if missing_group_refs:
        errors.append("sources missing platform group entries: " + ", ".join(missing_group_refs))
    return errors


def build_group_index(platform_groups: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for group in platform_groups.get("groups") or []:
        if not isinstance(group, dict):
            continue
        family = str(group.get("platform_family") or "")
        for group_source in group.get("sources") or []:
            if not isinstance(group_source, dict):
                continue
            source_key = str(group_source.get("source_key") or "")
            if not source_key:
                continue
            index[source_key] = {
                "platform_family": family,
                "group_title": group.get("title") or "",
                "group_status": group.get("status") or "",
                "shared_adapter_candidate": bool(group.get("shared_adapter_candidate")),
                "classification_confidence": group_source.get("classification_confidence") or "",
                "raw_platform": group_source.get("raw_platform") or "",
                "raw_access_method": group_source.get("raw_access_method") or "",
                "group_notes": group_source.get("notes") or "",
            }
    return index


def flatten_sources(target_catalog: dict[str, Any], group_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for jurisdiction in target_catalog.get("jurisdictions") or []:
        if not isinstance(jurisdiction, dict):
            continue
        jurisdiction_meta = {
            "jurisdiction_code": str(jurisdiction.get("code") or "").upper(),
            "jurisdiction_name": str(jurisdiction.get("name") or ""),
            "jurisdiction_type": str(jurisdiction.get("jurisdiction_type") or ""),
            "jurisdiction_aliases": [str(alias) for alias in jurisdiction.get("aliases") or []],
            "implementation_priority": str(jurisdiction.get("implementation_priority") or ""),
            "adapter_status": str(jurisdiction.get("adapter_status") or ""),
        }
        for source in jurisdiction.get("sources") or []:
            if not isinstance(source, dict):
                continue
            row = dict(source)
            row.update(jurisdiction_meta)
            row.update(group_index.get(str(source.get("source_key") or ""), {}))
            row.setdefault("platform_family", "")
            rows.append(row)
    return rows


def normalize_filter_values(value: str | list[str] | None) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = []
        for item in value:
            raw_values.extend(str(item).split(","))
    return {normalize_value(item) for item in raw_values if normalize_value(item)}


def normalize_value(value: Any) -> str:
    return str(value or "").strip().lower()


def contains_all(values: list[Any], requested: set[str]) -> bool:
    available = {normalize_value(value) for value in values}
    return requested <= available


def confidence_matches(source: dict[str, Any], requested: set[str]) -> bool:
    values = {
        normalize_value(source.get("confidence")),
        normalize_value(source.get("classification_confidence")),
    }
    return bool(values & requested)


def source_matches_state(source: dict[str, Any], value: str) -> bool:
    candidates = {
        normalize_value(source.get("jurisdiction_code")),
        normalize_value(source.get("jurisdiction_name")),
    }
    candidates.update(normalize_value(alias) for alias in source.get("jurisdiction_aliases") or [])
    return normalize_value(value) in candidates


def count_by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field) or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def count_members(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for value in row.get(field) or []:
            key = str(value or "unknown")
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def priority_sort_key(source: dict[str, Any]) -> tuple[int, str, str]:
    priority = normalize_value(source.get("implementation_priority"))
    return (
        PRIORITY_RANK.get(priority, 99),
        str(source.get("jurisdiction_code") or ""),
        str(source.get("source_key") or ""),
    )
