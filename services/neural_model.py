def analyze_opportunity(opportunity: dict) -> dict:
    """Placeholder neural model wrapper for the local POC."""
    keywords = opportunity.get("keywords_matched", [])
    risks = opportunity.get("risks", [])
    return {
        "recommendation": opportunity.get("ai_recommendation", "Monitor"),
        "summary": opportunity.get("summary", ""),
        "keywords": keywords,
        "risks": risks,
        "evidence": [
            {
                "claim": opportunity.get("eligibility_reason", "Review source document for eligibility."),
                "source_field": "Eligibility",
                "source_text": "Placeholder evidence generated from CSV fields.",
                "document_url": opportunity.get("document_url", ""),
            }
        ],
    }
