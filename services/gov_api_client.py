from datetime import datetime, timezone


def refresh_opportunities() -> dict:
    """Placeholder for .gov API integrations.

    Future implementations can call procurement or grant APIs with urllib.request,
    normalize results, and merge them into opportunities.csv.
    """
    return {
        "status": "stub",
        "message": "Refresh endpoint is wired. Add .gov API calls in services/gov_api_client.py.",
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "opportunities_added": 0,
    }
