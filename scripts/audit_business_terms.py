#!/usr/bin/env python3
"""Audit Python constants that duplicate centrally configured business terms."""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.search_taxonomy import load_search_taxonomy  # noqa: E402

SCAN_ROOTS = ("services", "scripts")
IGNORE_FILES = {"services/search_taxonomy.py", "scripts/audit_business_terms.py"}
SOURCE_MECHANIC_NAMES = {
    "SAM_TARGET_AGENCY_TERMS",
    "SAM_PTYPE_LABELS",
    "SAM_NOTICE_BUCKETS",
    "GRANTS_DEFAULT_AGENCIES",
    "FEDERAL_QUERY_CONTEXT_TERMS",
    "DEFAULT_AGENCY_SLUGS",
    "RELATED_API_NOTES",
    "CONTEXT_REQUIRED_KEYWORDS",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    name: str
    category: str
    matched_terms: tuple[str, ...]
    reason: str


def strings_in(node: ast.AST) -> Iterable[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        for child in ast.iter_child_nodes(node):
            yield from strings_in(child)


def assigned_name(node: ast.Assign | ast.AnnAssign) -> str:
    target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
    return target.id if isinstance(target, ast.Name) else ""


def classify(path: str, name: str) -> tuple[str, str]:
    if (
        name in SOURCE_MECHANIC_NAMES
        or name.startswith(("SAM_", "GRANTS_"))
        or (path == "services/gov_api_client.py" and name == "keep")
        or (path == "services/sam_entity_client.py" and name == "approved_live_vendor_keys")
        or (path == "services/federal_update_catalog.py" and name == "SOURCE_KEY_MAP")
    ):
        return "justified_source_specific", "required source query/filter vocabulary"
    if path.startswith(("services/state_opportunities/", "services/state_contracts/", "services/state_updates/")):
        if name == "TOPIC_RULES":
            return "migration_candidate", "shared state/federal classification vocabulary"
        return "justified_source_specific", "portal/parser context; caller still supplies business keywords"
    if name in {"KEYWORD_WEIGHTS", "TOPIC_RULES", "DEFAULT_VENDOR_ALIASES"}:
        return "migration_candidate", "shared classification or alias vocabulary"
    return "migration_candidate", "hardcoded monitored business terms"


def audit(root: Path = ROOT) -> list[Finding]:
    taxonomy = load_search_taxonomy(root / "data" / "search_parameters.json")
    known = {
        term.casefold(): term
        for term in [
            *taxonomy.business_terms,
            *taxonomy.rht_terms,
            *taxonomy.terms("health_programs", "gainwell_capabilities", "negative_noise"),
            *(alias for aliases in taxonomy.aliases_by_organization.values() for alias in aliases),
        ]
    }
    findings: list[Finding] = []
    for scan_root in SCAN_ROOTS:
        for file_path in sorted((root / scan_root).rglob("*.py")):
            relative = file_path.relative_to(root).as_posix()
            if relative in IGNORE_FILES:
                continue
            tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=relative)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                name = assigned_name(node)
                value = node.value
                if not name or value is None:
                    continue
                matched = []
                seen = set()
                for value_text in strings_in(value):
                    key = value_text.strip().casefold()
                    if key in known and key not in seen:
                        seen.add(key)
                        matched.append(known[key])
                if len(matched) < 2:
                    continue
                category, reason = classify(relative, name)
                findings.append(Finding(relative, node.lineno, name, category, tuple(matched), reason))
    return sorted(findings, key=lambda item: (item.category, item.path, item.line, item.name))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-candidates", action="store_true")
    args = parser.parse_args()
    findings = audit()
    if args.json:
        print(json.dumps([asdict(item) for item in findings], indent=2))
    else:
        for category in ("migration_candidate", "justified_source_specific"):
            selected = [item for item in findings if item.category == category]
            print(f"{category}: {len(selected)}")
            for item in selected:
                terms = ", ".join(item.matched_terms[:8])
                print(f"- {item.path}:{item.line} {item.name}: {terms} ({item.reason})")
    candidates = any(item.category == "migration_candidate" for item in findings)
    return 1 if args.fail_on_candidates and candidates else 0


if __name__ == "__main__":
    raise SystemExit(main())
