from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable

from services.state_http import fetch_url
from services.state_updates import state_update_record

MCWEB_BASE = "https://mcweb.apps.prd.cammis.medi-cal.ca.gov"
DHCS_NEWS_SITEMAP = "https://www.dhcs.ca.gov/dhcs-news-sitemap.xml"
MCWEB_ENV_URL = f"{MCWEB_BASE}/environment.js"
MCWEB_GRAPHQL_URL = f"{MCWEB_BASE}/graphql"

CONTEXT_TERMS = [
    "medi-cal",
    "medicaid",
    "dhcs",
    "calaim",
    "provider",
    "behavioral health",
    "managed care",
    "eligibility",
    "claims",
    "quality",
    "waiver",
    "state plan",
    "rural health",
]

NEWS_QUERY = """
query AllNewsArticlesPaged($offset: Int, $limit: Int) {
  news_articles(
    offset: $offset
    limit: $limit
    filter: {
      _and: [
        { publish_date: { _lte: "$NOW" } }
        { publish_date: { _gte: "$NOW(-3 years)" } }
        { _or: [{ publish_end_date: { _gte: "$NOW" } }, { publish_end_date: { _null: true } }] }
      ]
    }
    sort: ["-sort_date"]
  ) {
    id
    article_id
    article_title
    article_summary
    article_body
    publish_date
    revision_date
    publish_end_date
    category { categories_id { category_name } }
    community { communities_id { community_name community_abbrv } }
  }
  news_articles_aggregated(
    filter: {
      _and: [
        { publish_date: { _lte: "$NOW" } }
        { publish_date: { _gte: "$NOW(-3 years)" } }
        { _or: [{ publish_end_date: { _gte: "$NOW" } }, { publish_end_date: { _null: true } }] }
      ]
    }
  ) { count { id } }
}
"""

BULLETINS_QUERY = """
query RecentBulletins($limit: Int) {
  bulletins(
    limit: $limit
    sort: ["-publish_date"]
    filter: { _and: [{ publish_date: { _lte: "$NOW" } }, { publish_date: { _gte: "$NOW(-12 months)" } }] }
  ) {
    bulletin_id
    issue_number
    publish_date
    community { community_name community_abbrv }
    bulletin_content {
      bulletin_articles_bulletin_article_id {
        bulletin_article_id
        article_title
        article_body
        category { categories_id { category_name } }
        accordions { accordions_id { accordion_heading accordion_content } }
      }
    }
  }
  bulletins_aggregated(
    filter: { _and: [{ publish_date: { _lte: "$NOW" } }, { publish_date: { _gte: "$NOW(-12 months)" } }] }
  ) { count { bulletin_id } }
}
"""


def fetch_updates(
    *,
    keywords: list[str],
    max_records: int,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    scanned = 0

    news_rows = fetch_medi_cal_news(limit=min(max(max_records, 50), 150))
    scanned += len(news_rows)
    for row in news_rows:
        records.append(news_record(row, keywords))
    emit(progress, f"CA: scanned {len(news_rows)} Medi-Cal provider news rows")

    bulletin_rows = fetch_medi_cal_bulletins(limit=min(max(max_records // 2, 40), 80))
    scanned += len(bulletin_rows)
    seen_bulletins: set[str] = set()
    for row in bulletin_rows:
        article = row.get("article") or {}
        article_id = str(article.get("bulletin_article_id") or "").strip()
        if article_id and article_id in seen_bulletins:
            continue
        if article_id:
            seen_bulletins.add(article_id)
        records.append(bulletin_record(row, keywords))
    emit(progress, f"CA: scanned {len(bulletin_rows)} Medi-Cal provider bulletin article rows")

    sitemap_rows = fetch_dhcs_news_sitemap()
    scanned += len(sitemap_rows)
    for row in sitemap_rows:
        if is_relevant(row.get("title", ""), keywords):
            records.append(dhcs_sitemap_record(row, keywords))
    emit(progress, f"CA: scanned {len(sitemap_rows)} DHCS news sitemap rows")
    emit(progress, f"CA: normalized {len(records)} records from {scanned} scanned rows")
    return records[:max_records]


def fetch_medi_cal_news(*, limit: int) -> list[dict[str, Any]]:
    data = graphql(NEWS_QUERY, {"offset": 0, "limit": limit})
    return list((data.get("data") or {}).get("news_articles") or [])


def fetch_medi_cal_bulletins(*, limit: int) -> list[dict[str, Any]]:
    data = graphql(BULLETINS_QUERY, {"limit": limit})
    rows: list[dict[str, Any]] = []
    for bulletin in (data.get("data") or {}).get("bulletins") or []:
        for item in bulletin.get("bulletin_content") or []:
            article = item.get("bulletin_articles_bulletin_article_id") or {}
            if not article.get("article_title"):
                continue
            rows.append({"bulletin": bulletin, "article": article})
    return rows


def fetch_dhcs_news_sitemap() -> list[dict[str, str]]:
    result = fetch_url(
        DHCS_NEWS_SITEMAP,
        timeout=20,
        byte_limit=500_000,
        headers={"Accept": "application/xml,text/xml,*/*", "User-Agent": "Mozilla/5.0"},
    )
    result.raise_for_status()
    rows: list[dict[str, str]] = []
    for match in re.finditer(r"<url>\s*<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", result.body_text(), re.S):
        loc = clean_xml(match.group(1))
        lastmod = clean_xml(match.group(2))
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug == "news":
            continue
        rows.append({"loc": loc, "lastmod": lastmod, "slug": slug, "title": title_from_slug(slug)})
    return rows


def graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = fetch_directus_token()
    result = fetch_url(
        MCWEB_GRAPHQL_URL,
        method="POST",
        json_data={"query": query, "variables": variables},
        headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        timeout=35,
        byte_limit=5_000_000,
    )
    result.raise_for_status()
    data = json.loads(result.body_text())
    if data.get("errors"):
        raise RuntimeError(f"Medi-Cal GraphQL errors: {data['errors'][:2]}")
    return data


def fetch_directus_token() -> str:
    result = fetch_url(MCWEB_ENV_URL, timeout=15, byte_limit=5_000, headers={"Accept": "application/javascript,*/*"})
    result.raise_for_status()
    match = re.search(r'DIRECTUS_TOKEN=\s*"([^"]+)"', result.body_text())
    if not match:
        raise RuntimeError("Medi-Cal environment.js did not expose a public Directus token")
    return match.group(1)


def news_record(row: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    categories = category_names(row)
    communities = community_names(row)
    article_id = str(row.get("article_id") or row.get("id") or "").strip()
    summary = strip_html(row.get("article_summary") or row.get("article_body") or "")
    return state_update_record(
        state="CA",
        source="ca_medi_cal_provider_news",
        source_record_id=f"mcweb-news:{article_id}",
        record_type=ca_record_type(row.get("article_title", ""), summary, categories),
        title=str(row.get("article_title") or "Medi-Cal provider news"),
        agency="California Department of Health Care Services / Medi-Cal",
        summary=summary,
        posted_date=row.get("publish_date") or "",
        updated_date=row.get("revision_date") or "",
        document_url=first_document_url(row.get("article_body") or ""),
        source_url=f"{MCWEB_BASE}/news/{urllib.parse.quote(article_id)}" if article_id else f"{MCWEB_BASE}/publications/news",
        keywords=keywords,
        raw={"id": row.get("id"), "article_id": article_id, "categories": categories, "communities": communities},
    )


def bulletin_record(row: dict[str, Any], keywords: list[str]) -> dict[str, str]:
    bulletin = row.get("bulletin") or {}
    article = row.get("article") or {}
    body = strip_html(article.get("article_body") or "")
    article_id = str(article.get("bulletin_article_id") or "").strip()
    community = bulletin.get("community") or {}
    community_name = str(community.get("community_name") or "")
    issue_number = str(bulletin.get("issue_number") or "").strip()
    source_url = f"{MCWEB_BASE}/publications/bulletin?community={slugify(community_name)}"
    if issue_number:
        source_url += f"&issueNumber={urllib.parse.quote(issue_number)}"
    if article_id:
        source_url += f"&articleId={urllib.parse.quote(article_id)}"
    return state_update_record(
        state="CA",
        source="ca_medi_cal_provider_bulletins",
        source_record_id=f"mcweb-bulletin:{article_id or bulletin.get('bulletin_id')}",
        record_type="provider_bulletin",
        title=str(article.get("article_title") or "Medi-Cal provider bulletin"),
        agency="California Department of Health Care Services / Medi-Cal",
        summary=body,
        posted_date=bulletin.get("publish_date") or "",
        document_url=first_document_url(article.get("article_body") or ""),
        source_url=source_url,
        keywords=keywords,
        raw={
            "bulletin_id": bulletin.get("bulletin_id"),
            "issue_number": issue_number,
            "community": community,
            "article_id": article_id,
            "categories": category_names(article),
        },
    )


def dhcs_sitemap_record(row: dict[str, str], keywords: list[str]) -> dict[str, str]:
    title = row.get("title") or "DHCS news update"
    return state_update_record(
        state="CA",
        source="ca_dhcs_news_sitemap",
        source_record_id=f"dhcs-news:{row.get('slug', '')}",
        record_type=ca_record_type(title, "", []),
        title=title,
        agency="California Department of Health Care Services",
        summary="Official DHCS news update listed in the DHCS news XML sitemap.",
        updated_date=row.get("lastmod") or "",
        source_url=row.get("loc") or DHCS_NEWS_SITEMAP,
        keywords=keywords,
        raw={"sitemap": DHCS_NEWS_SITEMAP, "loc": row.get("loc"), "lastmod": row.get("lastmod")},
    )


def ca_record_type(title: Any, summary: Any, categories: list[str]) -> str:
    text = " ".join([str(title or ""), str(summary or ""), " ".join(categories)]).lower()
    if "bulletin" in text or "provider" in text:
        return "provider_bulletin"
    if "public comment" in text or "public notice" in text:
        return "public_comment_notice"
    if "waiver" in text or "1115" in text:
        return "waiver_notice"
    if "state plan amendment" in text or " spa" in f" {text}":
        return "spa_notice"
    if "grant" in text or "award" in text or "funding" in text:
        return "grant_notice"
    if "medi-cal" in text or "medicaid" in text or "dhcs notice" in text:
        return "medicaid_notice"
    return "policy_update"


def is_relevant(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords if keyword) or any(term in lower for term in CONTEXT_TERMS)


def category_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in row.get("category") or []:
        category = item.get("categories_id") or {}
        name = str(category.get("category_name") or "").strip()
        if name:
            names.append(name)
    return names


def community_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in row.get("community") or []:
        community = item.get("communities_id") or {}
        name = str(community.get("community_name") or "").strip()
        if name:
            names.append(name)
    return names


def first_document_url(html_text: str) -> str:
    match = re.search(r'href="([^"]+)"', str(html_text or ""), re.I)
    if not match:
        return ""
    return absolute_url(match.group(1))


def absolute_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    if text.startswith("/"):
        return MCWEB_BASE + text
    return urllib.parse.urljoin(MCWEB_BASE + "/", text)


def strip_html(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_xml(text)


def clean_xml(value: str) -> str:
    text = str(value or "")
    replacements = {"&amp;": "&", "&quot;": '"', "&#39;": "'", "&nbsp;": " ", "&lt;": "<", "&gt;": ">"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def title_from_slug(slug: str) -> str:
    words = re.sub(r"[-_]+", " ", slug).strip()
    return words[:1].upper() + words[1:] if words else "DHCS news update"


def slugify(value: str) -> str:
    text = str(value or "").replace("&", "and").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)
