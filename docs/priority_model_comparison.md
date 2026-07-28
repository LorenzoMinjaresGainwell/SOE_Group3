# Priority Model Comparison

Reproducible run: `python scripts/compare_priority_models.py --today 2026-07-28 --output docs/priority_model_comparison.md`

Model A uses 90% of the approved dimensional score plus 10% existing source score. Model B ignores the source score. Both then apply the same confidence penalty (maximum 2.5 points) and clamp to 0–100.

## Dataset and headline

- Canonical records scored: **5,730** (opportunities: 476, contracts: 538, updates: 4,716)
- Top-25 overlap: **22/25 (88.0%)**
- Overall Spearman rank correlation: **0.9740**

## Family comparison

| Family | N | A mean | B mean | Spearman | Top-25 overlap |
|---|---:|---:|---:|---:|---:|
| opportunities | 476 | 33.9 | 31.7 | 0.9843 | 25/25 |
| contracts | 538 | 44.4 | 45.8 | 0.9695 | 25/25 |
| updates | 4,716 | 51.4 | 53.2 | 0.9729 | 24/25 |

## Score distributions

| Family | Model | 0–19 | 20–39 | 40–59 | 60–79 | 80–100 |
|---|---|---:|---:|---:|---:|---:|
| opportunities | A | 96 | 243 | 103 | 32 | 2 |
| opportunities | B | 135 | 208 | 108 | 24 | 1 |
| contracts | A | 24 | 254 | 161 | 82 | 17 |
| contracts | B | 29 | 235 | 170 | 86 | 18 |
| updates | A | 0 | 80 | 3980 | 627 | 29 |
| updates | B | 0 | 63 | 3904 | 701 | 48 |

## RHT strength

RHT tiers are ordered **explicit > direct > related > generic > none** and are driven by checked-in terms, not inference.

| Family | Tier | All records | A top 25 | B top 25 |
|---|---|---:|---:|---:|
| opportunities | explicit | 19 | 16 | 16 |
| opportunities | direct | 2 | 0 | 0 |
| opportunities | related | 194 | 9 | 9 |
| opportunities | generic | 87 | 0 | 0 |
| opportunities | none | 174 | 0 | 0 |
| updates | explicit | 525 | 25 | 25 |
| updates | direct | 0 | 0 | 0 |
| updates | related | 4046 | 0 | 0 |
| updates | generic | 135 | 0 | 0 |
| updates | none | 10 | 0 | 0 |

## Missing-data diagnostics

Missing notes reduce confidence separately rather than silently changing a dimension's maximum.

| Family | Dimension | Missing | Rate |
|---|---|---:|---:|
| opportunities | rht | 174 | 36.6% |
| opportunities | capability | 95 | 20.0% |
| opportunities | strategic | 260 | 54.6% |
| opportunities | value | 422 | 88.7% |
| opportunities | urgency | 183 | 38.4% |
| contracts | timing | 216 | 40.1% |
| contracts | incumbent | 266 | 49.4% |
| contracts | value | 284 | 52.8% |
| contracts | health | 263 | 48.9% |
| contracts | strategic | 70 | 13.0% |
| updates | rht | 10 | 0.2% |
| updates | actionability | 4270 | 90.5% |
| updates | health | 340 | 7.2% |
| updates | strategic | 1697 | 36.0% |
| updates | recency | 0 | 0.0% |

## Manual review examples

Largest measurable rank shifts are shown for review; positive Δ means Model B ranks the record higher.

| Record | Family | Source score | A score/rank | B score/rank | B−A rank | RHT | Action |
|---|---|---:|---:|---:|---:|---|---|
| DFCM Maintenance - Stage II Janitorial Services - Invitation to Bid - Logan Workforce Serv | opportunities | 100 | 51.8 / 1918 | 46.6 / 4124 | -2205 | related | Qualify |
| Clinical Evaluations, Treatment, Wrap Service and Forensic Evaluations RFSQ | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |
| Services for DHHS Clients including People with ID.RC and/or ABI | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |
| Personal Emergency Response Systems and Medication Dispensary Devices | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |
| Massage Therapy Services | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |
| Peer Support for DSPD Clients and their Families | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |
| Adaptive Equipment for DSPD Clients | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |
| DHHS Emergency Placement Short-Term Shelter | opportunities | 100 | 51.2 / 2028 | 45.9 / 4144 | -2116 | related | Qualify |

## Recommendation

**Recommend Model B.** It is the clean scoring foundation because it measures only the approved dimensions; Model A partially reintroduces heterogeneous collector-era scores and therefore makes the same evidence worth different amounts by source. The models remain testably comparable (Spearman 0.9740, top-25 overlap 22/25), while Model B's top 25 contains 13 explicit/direct RHT records versus 11 for Model A. Review the rank-shift examples above before operational rollout.

This comparison makes no model/API quality claims. It evaluates deterministic formulas against available canonical fields.
