from __future__ import annotations

from typing import Callable


def blocked_fetcher(
    state: str,
    label: str,
    source_url: str,
    reason: str,
    *,
    progress: Callable[[str], None] | None,
) -> list[dict[str, str]]:
    """Report a source classification without attempting network access."""
    if progress:
        progress(f"{state} {label}: skipped: {reason} ({source_url})")
    return []
