#!/usr/bin/env python3
"""Compare deterministic priority Models A and B on canonical CSV records."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.priority_scoring import PriorityScorer  # noqa: E402


CANONICAL = {
    "opportunities": ("state_opportunities.csv", "federal_opportunities.csv", "federal_grants.csv"),
    "contracts": ("state_contracts.csv", "federal_contract_lifecycle.csv"),
    "updates": ("state_policy_updates.csv", "federal_updates_catalog.csv"),
}
ID_FIELDS = ("id", "opportunity_id", "grant_id", "contract_id", "update_id", "source_record_id")


def read_records(data_dir: Path):
    records = []
    for family, filenames in CANONICAL.items():
        for filename in filenames:
            path = data_dir / filename
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for line, row in enumerate(csv.DictReader(handle), 2):
                    if filename == "federal_updates_catalog.csv" and not relevant_update(row):
                        continue
                    enriched = dict(row)
                    enriched["scope"] = "state" if filename.startswith("state_") else "federal"
                    enriched["_file"] = filename
                    enriched["_key"] = f"{filename}:{next((row.get(k) for k in ID_FIELDS if row.get(k)), line)}"
                    if filename == "federal_grants.csv":
                        enriched["due_date"] = row.get("close_date", "")
                    records.append((family, enriched))
    return records


def relevant_update(row):
    record_type = row.get("record_type", "").strip().lower()
    constituent = record_type in {"opportunity", "grant", "award", "contract", "contract_award", "solicitation", "funding_opportunity"}
    constituent = constituent or any(row.get(field, "").strip() for field in ("opportunity_id", "grant_id", "contract_id"))
    try:
        score = float(row.get("importance_score") or row.get("relevance_score") or 0)
    except ValueError:
        score = 0
    return not constituent and score > 0


def rank_values(items):
    ordered = sorted(items, key=lambda item: (-item[1], item[0]))
    ranks = {}
    position = 1
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = (position + position + end - index - 1) / 2
        for key, _ in ordered[index:end]:
            ranks[key] = average
        position += end - index
        index = end
    return ranks


def correlation(left, right):
    keys = sorted(set(left) & set(right))
    if len(keys) < 2:
        return 0.0
    xs, ys = [left[k] for k in keys], [right[k] for k in keys]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return numerator / denominator if denominator else 1.0


def distribution(results):
    labels = ("0-19", "20-39", "40-59", "60-79", "80-100")
    counts = dict.fromkeys(labels, 0)
    for item in results:
        counts[labels[min(4, int(item["score"] // 20))]] += 1
    return counts


def pct(value, total):
    return f"{100 * value / total:.1f}%" if total else "n/a"


def build_report(records, scorer, today):
    scored = {"A": [], "B": []}
    raw_by_key = {}
    for family, record in records:
        raw_by_key[record["_key"]] = record
        for model in scored:
            result = scorer.score(record, family, model=model, today=today)
            result.update({"key": record["_key"], "title": record.get("title") or record.get("opportunity_title") or "Untitled"})
            scored[model].append(result)

    ranks = {model: rank_values([(r["key"], r["score"]) for r in results]) for model, results in scored.items()}
    top = {model: sorted(results, key=lambda r: (-r["score"], r["key"]))[:25] for model, results in scored.items()}
    overlap = {r["key"] for r in top["A"]} & {r["key"] for r in top["B"]}
    lines = [
        "# Priority Model Comparison", "",
        f"Reproducible run: `python scripts/compare_priority_models.py --today {today.isoformat()} --output docs/priority_model_comparison.md`", "",
        "Model A uses 90% of the approved dimensional score plus 10% existing source score. Model B ignores the source score. Both then apply the same confidence penalty (maximum 2.5 points) and clamp to 0–100.", "",
        "## Dataset and headline", "",
        f"- Canonical records scored: **{len(records):,}** (" + ", ".join(f"{family}: {sum(f == family for f, _ in records):,}" for family in CANONICAL) + ")",
        f"- Top-25 overlap: **{len(overlap)}/25 ({pct(len(overlap), 25)})**",
        f"- Overall Spearman rank correlation: **{correlation(ranks['A'], ranks['B']):.4f}**",
        "",
        "## Family comparison", "",
        "| Family | N | A mean | B mean | Spearman | Top-25 overlap |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for family in CANONICAL:
        groups = {m: [r for r in scored[m] if r["family"] == family] for m in scored}
        family_ranks = {m: rank_values([(r["key"], r["score"]) for r in groups[m]]) for m in scored}
        family_top = {m: {r["key"] for r in sorted(groups[m], key=lambda r: (-r["score"], r["key"]))[:25]} for m in scored}
        lines.append(f"| {family} | {len(groups['A']):,} | {statistics.mean(r['score'] for r in groups['A']):.1f} | {statistics.mean(r['score'] for r in groups['B']):.1f} | {correlation(family_ranks['A'], family_ranks['B']):.4f} | {len(family_top['A'] & family_top['B'])}/25 |")

    lines += ["", "## Score distributions", "", "| Family | Model | 0–19 | 20–39 | 40–59 | 60–79 | 80–100 |", "|---|---|---:|---:|---:|---:|---:|"]
    for family in CANONICAL:
        for model in scored:
            dist = distribution([r for r in scored[model] if r["family"] == family])
            lines.append(f"| {family} | {model} | " + " | ".join(str(dist[key]) for key in ("0-19", "20-39", "40-59", "60-79", "80-100")) + " |")

    lines += ["", "## RHT strength", "", "RHT tiers are ordered **explicit > direct > related > generic > none** and are driven by checked-in terms, not inference.", "", "| Family | Tier | All records | A top 25 | B top 25 |", "|---|---|---:|---:|---:|"]
    for family in ("opportunities", "updates"):
        all_counts = Counter(r["rht_strength"] for r in scored["B"] if r["family"] == family)
        family_top = {
            model: sorted((r for r in scored[model] if r["family"] == family),
                          key=lambda r: (-r["score"], r["key"]))[:25]
            for model in scored
        }
        for tier in ("explicit", "direct", "related", "generic", "none"):
            lines.append(f"| {family} | {tier} | {all_counts[tier]} | {sum(r['rht_strength'] == tier for r in family_top['A'])} | {sum(r['rht_strength'] == tier for r in family_top['B'])} |")

    lines += ["", "## Missing-data diagnostics", "", "Missing notes reduce confidence separately rather than silently changing a dimension's maximum.", "", "| Family | Dimension | Missing | Rate |", "|---|---|---:|---:|"]
    for family in CANONICAL:
        family_results = [r for r in scored["B"] if r["family"] == family]
        misses = Counter(d["dimension"] for r in family_results for d in r["dimensions"] if d["missing_notes"])
        for dimension in family_results[0]["dimensions"]:
            count = misses[dimension["dimension"]]
            lines.append(f"| {family} | {dimension['dimension']} | {count} | {pct(count, len(family_results))} |")

    shifts = sorted(scored["B"], key=lambda r: (-abs(ranks["A"][r["key"]] - ranks["B"][r["key"]]), r["key"]))[:8]
    lines += ["", "## Manual review examples", "", "Largest measurable rank shifts are shown for review; positive Δ means Model B ranks the record higher.", "", "| Record | Family | Source score | A score/rank | B score/rank | B−A rank | RHT | Action |", "|---|---|---:|---:|---:|---:|---|---|"]
    by_model = {m: {r["key"]: r for r in scored[m]} for m in scored}
    for item in shifts:
        key = item["key"]
        a, b = by_model["A"][key], by_model["B"][key]
        delta = ranks["A"][key] - ranks["B"][key]
        title = b["title"].replace("|", "\\|")[:90]
        source = "missing" if b["source_score"] is None else f"{b['source_score']:.0f}"
        lines.append(f"| {title} | {b['family']} | {source} | {a['score']:.1f} / {ranks['A'][key]:.0f} | {b['score']:.1f} / {ranks['B'][key]:.0f} | {delta:+.0f} | {b['rht_strength']} | {b['action']} |")

    explicit_a = sum(r["rht_strength"] in {"explicit", "direct"} for r in top["A"])
    explicit_b = sum(r["rht_strength"] in {"explicit", "direct"} for r in top["B"])
    lines += ["", "## Recommendation", "",
              "**Recommend Model B.** It is the clean scoring foundation because it measures only the approved dimensions; Model A partially reintroduces heterogeneous collector-era scores and therefore makes the same evidence worth different amounts by source. " +
              f"The models remain testably comparable (Spearman {correlation(ranks['A'], ranks['B']):.4f}, top-25 overlap {len(overlap)}/25), while Model B's top 25 contains {explicit_b} explicit/direct RHT records versus {explicit_a} for Model A. Review the rank-shift examples above before operational rollout.", "",
              "This comparison makes no model/API quality claims. It evaluates deterministic formulas against available canonical fields.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--today", type=date.fromisoformat, required=True, help="ISO date; required for reproducibility")
    parser.add_argument("--output", type=Path, help="write Markdown instead of stdout")
    args = parser.parse_args()
    report = build_report(read_records(args.data_dir), PriorityScorer(args.data_dir), args.today)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)


if __name__ == "__main__":
    main()
