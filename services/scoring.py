def explain_fit_score(opportunity: dict, rules: list[dict]) -> list[dict]:
    score = int(opportunity.get("fit_score", 0))
    weights = {rule["category"]: int(rule["weight"]) for rule in rules}

    alignment = min(weights.get("Strategic alignment", 50), round(score * 0.52))
    budget = min(weights.get("Budget size", 25), round(score * 0.25))
    eligibility = min(weights.get("Eligibility confidence", 15), round(score * 0.16))
    risk = max(0, score - alignment - budget - eligibility)

    return [
        {"label": "Strategic alignment", "score": alignment, "max": weights.get("Strategic alignment", 50)},
        {"label": "Budget size", "score": budget, "max": weights.get("Budget size", 25)},
        {"label": "Eligibility confidence", "score": eligibility, "max": weights.get("Eligibility confidence", 15)},
        {"label": "Risk factors", "score": min(risk, weights.get("Risk factors", 10)), "max": weights.get("Risk factors", 10)},
    ]
