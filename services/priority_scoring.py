"""Deterministic, family-aware priority scoring.

This module uses only record fields and checked-in CSV configuration.  It makes no
model or API calls.  Callers should inject ``today`` for reproducible results.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable


FAMILY_WEIGHTS = {
    "opportunities": {"rht": 35, "capability": 25, "strategic": 15, "value": 15, "urgency": 10},
    "contracts": {"timing": 30, "incumbent": 20, "value": 20, "health": 20, "strategic": 10},
    "updates": {"rht": 40, "actionability": 20, "health": 20, "strategic": 10, "recency": 10},
}
RHT_TIERS = ("explicit", "direct", "related", "generic", "none")
TEXT_FIELDS = (
    "title", "description", "summary", "agency", "subagency", "office",
    "record_type", "document_type", "notice_type", "opportunity_type",
    "program_focus", "topic_keys", "matched_keywords", "keywords_matched",
    "capability", "capabilities", "capability_keys", "capability_areas",
)
RULE_CATEGORIES = frozenset({"rht", "capability"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    tier: str
    terms: tuple[str, ...]
    strength: float
    description: str


class PriorityScorer:
    """Score opportunities, contracts, and updates with explainable dimensions."""

    def __init__(self, config_dir: str | Path | None = None) -> None:
        root = Path(config_dir) if config_dir else Path(__file__).resolve().parents[1] / "data"
        self.rules = self._load_rules(root / "capability_rules.csv")
        self.jurisdictions = self._load_jurisdictions(root / "strategic_jurisdictions.csv")

    def score(
        self,
        record: dict[str, Any],
        family: str,
        *,
        model: str = "B",
        today: date | None = None,
    ) -> dict[str, Any]:
        family = {"opportunity": "opportunities", "contract": "contracts", "update": "updates"}.get(
            family.lower(), family.lower()
        )
        if family not in FAMILY_WEIGHTS:
            raise ValueError(f"unsupported family: {family}")
        model = model.upper()
        if model not in {"A", "B"}:
            raise ValueError("model must be A or B")
        today = today or date.today()

        factors = self._factors(record, family, today)
        dimensions = []
        for name, maximum in FAMILY_WEIGHTS[family].items():
            factor, evidence, missing = factors[name]
            dimensions.append({
                "dimension": name,
                "score": round(maximum * max(0.0, min(1.0, factor)), 1),
                "max": maximum,
                "evidence": evidence,
                "missing_notes": missing,
            })
        dimensional_score = round(sum(item["score"] for item in dimensions), 1)
        source_score = self._source_score(record)
        # Model A reserves 10% for the old source score while retaining the
        # approved dimensions in their original relative proportions.
        before_confidence = dimensional_score
        source_note = "ignored by Model B"
        if model == "A":
            used_source = dimensional_score if source_score is None else source_score
            before_confidence = round(dimensional_score * 0.90 + used_source * 0.10, 1)
            source_note = "missing; dimensional score substituted" if source_score is None else "10% source score"

        missing_count = sum(bool(item["missing_notes"]) for item in dimensions)
        confidence_value = max(0.50, round(1.0 - 0.11 * missing_count, 2))
        penalty = round((1.0 - confidence_value) * 5.0, 1)
        confidence = {
            "value": confidence_value,
            "label": "high" if confidence_value >= 0.85 else "medium" if confidence_value >= 0.65 else "low",
            "penalty": penalty,
            "missing_dimension_count": missing_count,
        }
        final_score = round(max(0.0, min(100.0, before_confidence - penalty)), 1)
        rht_tier = self._rht(record)[0]
        return {
            "family": family,
            "model": model,
            "score": final_score,
            "dimensional_score": dimensional_score,
            "dimensions": dimensions,
            "source_score": source_score,
            "source_score_note": source_note,
            "confidence": confidence,
            "action": self._action(record, family, final_score, today),
            "rht_strength": rht_tier,
            "scored_as_of": today.isoformat(),
        }

    def _factors(self, record: dict[str, Any], family: str, today: date):
        strategic = self._strategic(record)
        value = self._value(record)
        if family == "opportunities":
            tier, strength, evidence = self._rht(record)
            capability = self._capability(record)
            return {
                "rht": (strength, evidence, "" if tier != "none" else "No RHT or health signal"),
                "capability": capability,
                "strategic": strategic,
                "value": value,
                "urgency": self._opportunity_urgency(record, today),
            }
        if family == "contracts":
            return {
                "timing": self._contract_timing(record, today),
                "incumbent": self._incumbent(record),
                "value": value,
                "health": self._health(record, family),
                "strategic": strategic,
            }
        tier, strength, evidence = self._rht(record)
        return {
            "rht": (strength, evidence, "" if tier != "none" else "No RHT or health signal"),
            "actionability": self._actionability(record, today),
            "health": self._health(record, family),
            "strategic": strategic,
            "recency": self._recency(record, today),
        }

    def _rht(self, record: dict[str, Any]) -> tuple[str, float, list[str]]:
        if _truthy(record.get("rht_flag")):
            return "explicit", 1.0, ["rht_flag=true"]
        text = _record_text(record)
        for tier in RHT_TIERS[:-1]:
            matches = []
            strengths = []
            for rule in self.rules:
                if rule.category == "rht" and rule.tier == tier:
                    hit = _matched_terms(text, rule.terms)
                    if hit:
                        matches.extend(hit)
                        strengths.append(rule.strength)
            if matches:
                return tier, max(strengths), sorted(set(matches))
        return "none", 0.0, []

    def _capability(self, record: dict[str, Any]) -> tuple[float, list[str], str]:
        text = _record_text(record)
        hits = []
        strengths = []
        for rule in self.rules:
            if rule.category != "capability":
                continue
            terms = _matched_terms(text, rule.terms)
            if terms:
                hits.append(f"{rule.rule_id}: {', '.join(terms)}")
                strengths.append(rule.strength)
        factor = min(1.0, sum(sorted(strengths, reverse=True)[:3]) / 2.0)
        return factor, hits, "" if hits else "No configured capability match"

    def _strategic(self, record: dict[str, Any]) -> tuple[float, list[str], str]:
        jurisdiction = str(record.get("state") or record.get("jurisdiction") or "").strip().upper()
        if not jurisdiction and _is_federal(record):
            jurisdiction = "US"
        configured = self.jurisdictions.get(jurisdiction)
        if configured:
            priority, reason = configured
            return priority, [f"{jurisdiction}: {reason}"], ""
        if jurisdiction:
            return 0.35, [f"{jurisdiction}: baseline jurisdiction"], "Not in strategic jurisdiction config"
        return 0.0, [], "Jurisdiction missing"

    def _value(self, record: dict[str, Any]) -> tuple[float, list[str], str]:
        fields = ("predictive_value_usd", "potential_total_value", "current_total_value", "award_amount",
                  "estimated_total_program_funding", "award_ceiling", "budget_estimate", "amount")
        amounts = [_number(record.get(field)) for field in fields]
        amount = max((item for item in amounts if item is not None), default=None)
        if amount is None or amount <= 0:
            return 0.0, [], "Monetary value missing"
        # Continuous log scale: $10K begins to matter and $100M reaches full credit.
        factor = max(0.10, min(1.0, (math.log10(amount) - 4.0) / 4.0))
        return factor, [f"value=${amount:,.0f}"], ""

    def _opportunity_urgency(self, record: dict[str, Any], today: date):
        due = _first_date(record, "due_date", "close_date", "action_required_by")
        if not due:
            return 0.0, [], "Due date missing"
        days = (due - today).days
        if days < 0:
            factor = 0.0
        elif days < 15:
            factor = 0.45 + days * (0.55 / 15)
        elif days <= 45:
            factor = 1.0
        elif days <= 90:
            factor = 1.0 - (days - 45) * (0.45 / 45)
        elif days <= 180:
            factor = 0.55 - (days - 90) * (0.35 / 90)
        else:
            factor = 0.10
        return factor, [f"due={due.isoformat()}", f"days={days}", "peak=15-45 days"], ""

    def _contract_timing(self, record: dict[str, Any], today: date):
        days = _integer(record.get("days_until_end"))
        end = _first_date(record, "potential_end_date", "period_end_date", "end_date")
        if days is None and end:
            days = (end - today).days
        if days is None and _number(record.get("months_to_end")) is not None:
            days = round(_number(record.get("months_to_end")) * 30.4375)
        if days is None:
            return 0.0, [], "Contract end timing missing"
        if days < 0:
            factor = 0.10
        elif days <= 180:
            factor = 1.0
        elif days <= 365:
            factor = 0.80
        elif days <= 730:
            factor = 0.50
        else:
            factor = 0.20
        return factor, [f"days_until_end={days}"], ""

    def _incumbent(self, record: dict[str, Any]):
        relation = " ".join(str(record.get(key) or "") for key in
                            ("gwt_relation", "known_bid_status", "vendor_name", "incumbent_vendor_key")).lower()
        if any(term in relation for term in ("gainwell", " gwt", "incumbent_us", "our contract")):
            return 1.0, ["Gainwell/own incumbent relationship"], ""
        if _truthy(record.get("competitor_flag")) or "competitor" in relation:
            return 0.85, ["competitor incumbent"], ""
        if relation.strip():
            return 0.35, ["incumbent or vendor identified"], "Relationship not classified"
        return 0.0, [], "Incumbent relationship missing"

    def _health(self, record: dict[str, Any], family: str):
        fields = (("title", "agency", "vendor_name", "end_date|period_end_date|potential_end_date", "amount|award_amount|current_total_value")
                  if family == "contracts" else
                  ("title", "agency", "posted_date|updated_date", "source_url|document_url", "summary|topic_keys|program_focus"))
        present, absent = [], []
        for alternatives in fields:
            choices = alternatives.split("|")
            if any(str(record.get(key) or "").strip() for key in choices):
                present.append(alternatives)
            else:
                absent.append(alternatives)
        return len(present) / len(fields), present, "" if not absent else "Missing: " + ", ".join(absent)

    def _actionability(self, record: dict[str, Any], today: date):
        deadline = _first_date(record, "action_required_by", "due_date", "comment_close_date")
        comment = _truthy(record.get("comment_required_flag")) or "comment" in _record_text(record)
        if deadline:
            days = (deadline - today).days
            factor = 1.0 if 0 <= days <= 45 else 0.75 if 46 <= days <= 90 else 0.30 if days > 90 else 0.10
            if comment:
                factor = min(1.0, factor + 0.15)
            return factor, [f"deadline={deadline.isoformat()}", f"days={days}", f"comment_required={comment}"], ""
        if comment:
            return 0.55, ["comment signal without deadline"], "Action deadline missing"
        return 0.15, [], "Action requirement and deadline missing"

    def _recency(self, record: dict[str, Any], today: date):
        seen = _first_date(record, "updated_date", "posted_date", "publication_date", "last_checked_at")
        if not seen:
            return 0.0, [], "Record date missing"
        age = max(0, (today - seen).days)
        factor = 1.0 if age <= 30 else 0.75 if age <= 90 else 0.45 if age <= 180 else 0.20 if age <= 365 else 0.05
        return factor, [f"record_date={seen.isoformat()}", f"age_days={age}"], ""

    def _action(self, record: dict[str, Any], family: str, score: float, today: date) -> str:
        if family == "contracts":
            days = _integer(record.get("days_until_end"))
            end = _first_date(record, "potential_end_date", "period_end_date", "end_date")
            if days is None and end:
                days = (end - today).days
            if days is None and _number(record.get("months_to_end")) is not None:
                days = round(_number(record.get("months_to_end")) * 30.4375)
            if days is not None and days < 0:
                return "Historical"
            own = self._incumbent(record)[0] == 1.0
            if own and (days is None or days <= 730):
                return "Retain"
            if (_truthy(record.get("competitor_flag")) or self._incumbent(record)[0] >= 0.85) and days is not None and days <= 365:
                return "Compete"
            if days is not None and days <= 730:
                return "Prepare"
            return "Monitor"
        if family == "updates":
            deadline = _first_date(record, "action_required_by", "due_date", "comment_close_date")
            days = (deadline - today).days if deadline else None
            if days is not None and 0 <= days <= 45:
                return "Act"
            if score >= 60 or _truthy(record.get("comment_required_flag")):
                return "Review"
            if score >= 30:
                return "Monitor"
            return "Informational"
        return "Pursue" if score >= 70 else "Qualify" if score >= 45 else "Monitor"

    @staticmethod
    def _source_score(record: dict[str, Any]) -> float | None:
        for key in ("importance_score", "relevance_score", "fit_score", "source_relevance_score"):
            value = _number(record.get(key))
            if value is not None:
                return max(0.0, min(100.0, value))
        return None

    @staticmethod
    def _load_rules(path: Path) -> list[Rule]:
        required = {"rule_id", "category", "tier", "terms", "strength", "description"}
        rules: list[Rule] = []
        seen: set[str] = set()
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"invalid capability rules config: missing columns in {path}")
            for row_number, row in enumerate(reader, start=2):
                rule_id = (row.get("rule_id") or "").strip()
                category = (row.get("category") or "").strip().lower()
                tier = (row.get("tier") or "").strip().lower()
                terms = tuple(term.strip().lower() for term in (row.get("terms") or "").split("|") if term.strip())
                description = (row.get("description") or "").strip()
                try:
                    strength = float(row.get("strength") or "")
                except ValueError as exc:
                    raise ValueError(f"invalid strength on capability rule row {row_number}") from exc
                if (not rule_id or rule_id in seen or category not in RULE_CATEGORIES or
                        tier not in RHT_TIERS[:-1] or not terms or not description or
                        not math.isfinite(strength) or not 0 <= strength <= 1):
                    raise ValueError(f"invalid capability rule row {row_number}")
                seen.add(rule_id)
                rules.append(Rule(rule_id, category, tier, terms, strength, description))
        if not rules:
            raise ValueError(f"capability rules config is empty: {path}")
        return rules

    @staticmethod
    def _load_jurisdictions(path: Path) -> dict[str, tuple[float, str]]:
        required = {"jurisdiction", "priority", "reason"}
        result: dict[str, tuple[float, str]] = {}
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise ValueError(f"invalid strategic jurisdictions config: missing columns in {path}")
            for row_number, row in enumerate(reader, start=2):
                jurisdiction = (row.get("jurisdiction") or "").strip().upper()
                reason = (row.get("reason") or "").strip()
                try:
                    priority = float(row.get("priority") or "")
                except ValueError as exc:
                    raise ValueError(f"invalid priority on jurisdiction row {row_number}") from exc
                if (not jurisdiction or jurisdiction in result or not reason or
                        not math.isfinite(priority) or not 0 <= priority <= 1):
                    raise ValueError(f"invalid strategic jurisdiction row {row_number}")
                result[jurisdiction] = (priority, reason)
        if not result:
            raise ValueError(f"strategic jurisdictions config is empty: {path}")
        return result


def _record_text(record: dict[str, Any]) -> str:
    """Build scoring text solely from reviewed, meaningful content fields."""
    values: list[str] = []
    for key in TEXT_FIELDS:
        value = record.get(key)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value if isinstance(item, (str, int, float, bool)))
        elif isinstance(value, (str, int, float, bool)):
            values.append(str(value))
    return " ".join(values).lower()


def _matched_terms(text: str, terms: Iterable[str]) -> list[str]:
    return [term for term in terms if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text)]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(str(value).replace("$", "").replace(",", ""))
        return number if math.isfinite(number) else None
    except (ValueError, OverflowError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text):
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            pass
    for pattern in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


def _first_date(record: dict[str, Any], *keys: str) -> date | None:
    for key in keys:
        parsed = _parse_date(record.get(key))
        if parsed:
            return parsed
    return None


def _is_federal(record: dict[str, Any]) -> bool:
    scope = str(record.get("scope") or "").lower()
    source = str(record.get("source_key") or record.get("source") or "").lower()
    return scope == "federal" or source.startswith(("sam", "federal", "grants", "usaspending", "cms"))
