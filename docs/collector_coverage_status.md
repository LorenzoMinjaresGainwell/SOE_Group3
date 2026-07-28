# Collector coverage status

Status after the nationwide backend expansion. Active means the adapter is registered and can run against an official public source. Blocked adapters remain unregistered so they are not presented as live coverage.

## Active coverage

| Family | Active jurisdictions | Total target jurisdictions |
|---|---:|---:|
| New opportunities | 48 | 56 |
| Contracts and recompetes | 34 | 56 |
| Medicaid, Medicare, CMS, and RHT updates | 47 | 56 |

The target set is the 50 states, District of Columbia, American Samoa, Guam, Northern Mariana Islands, Puerto Rico, and U.S. Virgin Islands.

## Contract sources not active

Officially blocked or unsuitable for automated post-award collection:

- CT: protected/stateful portal; no stable award feed
- HI: client-rendered HANDS awards; no verified stable public feed
- KS: Oracle supplier login/cookie check
- KY: client-rendered SharePoint search; no stable public feed
- ME: no verified post-award vendor/term feed
- MN: Radware CAPTCHA
- MS: former search unavailable; no replacement award feed
- MT: no verified post-award vendor/term feed
- NE: no verified post-award vendor/term feed
- NV: vendor and term data require unbounded detail-page crawling
- NH: access-denied/stateful ASP.NET search
- NM: official Sunshine Portal rejects public probes
- ND: enterprise reCAPTCHA/browser check
- RI: public source is solicitation-oriented, not post-award
- SC: terms are public, but awarded vendors are not exposed
- SD: stateful ASP.NET search without a stable feed
- VI: no verified post-award vendor/term feed
- OH: OhioBuys browser-check/reCAPTCHA; no alternate public feed
- WI: VendorNet exposes only a Telerik/ASP.NET AJAX shell
- AS: official site/notices/sitemaps expose no current-contract feed
- GU: official GSA listing lacks unambiguous current-contract fields
- MP: official routes redirect to login/upgrades or an unavailable RFP host

## Update sources not active

Officially blocked:

- DE: provider bulletin endpoint loops on cookie detection
- KS: KanCare publication source returns challenge/403
- MA: official Mass.gov page/API returns 403
- MN: Radware Bot Manager challenge
- NH: official source returns challenge/403
- OH: official Medicaid update routes return 404 shells
- WI: ForwardHealth reports the requested update resources unavailable
- AS: official sources expose no stable Medicaid update feed
- GU: Medicaid/public-health hosts are unavailable and the official content API returned no records

## Interpretation

- **Active with records:** a bounded live run produced normalized rows.
- **Active, verified zero:** the official source ran successfully, but the current query/window produced no qualifying rows.
- **Blocked:** access controls or lack of a stable official machine-readable/simple HTML post prevents responsible automation.
- **Unresolved:** final source confirmation is still in progress; it is not counted as coverage.

Blocked and zero-result states do not mean the jurisdiction has no relevant activity. Users should follow the linked official portals when automated coverage is unavailable.
