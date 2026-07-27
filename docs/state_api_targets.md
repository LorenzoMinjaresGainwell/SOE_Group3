# State API Targets
Machine source: `data/state_api_targets.json`.

Purpose: first-pass adapter target map for state and territory procurement, contracts, awards, and Medicaid/CMS/RHT-relevant updates. Treat `target_api` as the target adapter surface. Only `official_portal_json` entries are known JSON endpoints today.

## Coverage

- Jurisdictions: 56 (50 states, DC, AS, GU, MP, PR, VI)
- Current implemented MVP: PA, TX
- MVP rule: max two portal families per jurisdiction: one opportunities source, one contract/incumbent source

## Tags

- `opportunities` - Open solicitations, RFPs, RFIs, bids, sources sought, funding opportunities.
- `contracts` - Awarded/current statewide contracts or contract records.
- `awards` - Award notices, apparent awards, award history, bid tabulations.
- `incumbents` - Vendor/party searchable contract history useful for incumbent tracking.
- `expiring_contracts` - Records with end dates, renewals, or term metadata useful for recompete windows.
- `procurement_updates` - Addenda, amendments, cancellation notices, award updates, portal change notices.
- `grants` - State grant/funding postings where present in the procurement source.
- `medicaid_updates` - State Medicaid/health program notices or procurement updates relevant to Medicaid/CMS work.
- `rht_updates` - Rural health transformation, rural hospital, CAH, workforce, telehealth, or rural funding signals.
- `open_data` - Socrata/open-data/catalog API or machine-readable public data endpoint.

## Jurisdictions

| Jurisdiction | Code | Priority | Source | Access | Tags | URL | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Alabama | AL | medium | Alabama Buys | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://alabamabuys.gov/ | Primary statewide procurement target. |
| Alaska | AK | medium | Alaska Online Public Notices | `html_search_adapter` | opportunities, procurement_updates, medicaid_updates | https://aws.state.ak.us/OnlinePublicNotices/ | Good for notices; contract history may require IRIS/VSS or agency pages. |
|  |  |  | Alaska IRIS Vendor Self Service | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://iris-vss.alaska.gov/webapp/PRDVSS1X1/AltSelfService | Secondary target for richer procurement metadata. |
| American Samoa | AS | low | American Samoa Government Procurement | `manual_probe_required` | opportunities, procurement_updates | https://www.americansamoa.gov/ | Territory source availability likely sparse; start with official ASG procurement/treasury pages. |
| Arizona | AZ | high | Arizona Procurement Portal | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://app.az.gov/ | High-value Medicaid/MMIS state; prioritize after proven adapters. |
| Arkansas | AR | medium | Arkansas Vendor Services | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://www.arkansasvendorservices.com/ | Use DFA/TSS procurement pages as fallback for statewide contracts. |
| California | CA | high | Cal eProcure | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates, grants | https://caleprocure.ca.gov/ | Large Medicaid/Medi-Cal market; high priority. |
| Colorado | CO | high | Colorado Vendor Self Service | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://codpa-vss.cloud.cgifederal.com/webapp/PRDVSS2X1/AltSelfService | High Medicaid modernization relevance; check BidNet if VSS is incomplete. |
| Connecticut | CT | medium | CTsource | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://portal.ct.gov/das/ctsource | Portal may route into Jaggaer/BidSync-style event pages. |
| Delaware | DE | medium | MyMarketplace Delaware | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://mymarketplace.delaware.gov/ | Small state; include for completeness and regional Medicaid monitoring. |
| District of Columbia | DC | medium | DC Office of Contracting and Procurement | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates, open_data | https://contracts.ocp.dc.gov/ | Check DC Open Data Socrata for contract mirrors if portal JS is heavy. |
| Florida | FL | high | MyFloridaMarketPlace Vendor Information Portal | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://vendor.myfloridamarketplace.com/ | High-value Medicaid and managed care market. |
| Georgia | GA | high | Georgia Procurement Registry | `html_form_adapter` | opportunities, awards, contracts, procurement_updates | https://ssl.doas.state.ga.us/PRSapp/PR_index.jsp | High-value Medicaid/MMIS market. |
| Guam | GU | low | Guam General Services Agency Procurement | `manual_probe_required` | opportunities, contracts, procurement_updates | https://gsa.guam.gov/ | Territory source; likely HTML/document parsing. |
| Hawaii | HI | medium | Hawaii Awards and Notices Data System | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://hands.ehawaii.gov/hands/ | One portal covers opportunities and award notices. |
| Idaho | ID | medium | Idaho Division of Purchasing | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://purchasing.idaho.gov/ | Start from official purchasing site and follow public event links. |
| Illinois | IL | high | Illinois BidBuy | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://www.bidbuy.illinois.gov/bso/ | High Medicaid/enterprise procurement priority. |
| Indiana | IN | high | Indiana IDOA Procurement | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://www.in.gov/idoa/procurement/ | High Medicaid systems relevance. |
| Iowa | IA | medium | Iowa DAS Procurement | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://das.iowa.gov/procurement | Likely Jaggaer/SciQuest event backend for current solicitations. |
| Kansas | KS | medium | Kansas eSupplier | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://supplier.sok.ks.gov/psc/sokfsprdsup/SUPPLIER/ERP/c/NUI_FRAMEWORK.PT_LANDINGPAGE.GBL | PeopleSoft state can require session/form-state handling. |
| Kentucky | KY | high | Kentucky Vendor Self Service | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://emars.ky.gov/online/vss/ | High Medicaid/MMIS and MCO market. |
| Louisiana | LA | high | Louisiana LaPAC | `html_form_adapter` | opportunities, procurement_updates | https://wwwcfprd.doa.louisiana.gov/osp/lapac/pubMain.cfm | Pair with LaGov/vendor pages for contract history if needed. |
| Maine | ME | medium | Maine Procurement Services RFPs | `html_table_adapter` | opportunities, awards, procurement_updates | https://www.maine.gov/dafs/bbm/procurementservices/vendors/rfps | HTML/document-heavy; cap source count for MVP. |
| Maryland | MD | high | eMaryland Marketplace Advantage | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://emma.maryland.gov/ | High healthcare and Medicaid procurement priority. |
| Massachusetts | MA | high | COMMBUYS | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://www.commbuys.com/bso/ | High Medicaid and health IT priority. |
| Michigan | MI | high | Michigan SIGMA Vendor Self Service | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://sigma.michigan.gov/webapp/PRDVSS2X1/AltSelfService | High Medicaid/eligibility systems priority. |
| Minnesota | MN | high | Minnesota SWIFT Supplier Portal | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://supplier.swift.state.mn.us/ | High health and eligibility systems priority. |
| Mississippi | MS | medium | Mississippi DFA Contract and Bid Search | `html_search_adapter` | opportunities, awards, contracts, procurement_updates | https://www.ms.gov/dfa/contract_bid_search | Good candidate for lightweight HTML/query adapter. |
| Missouri | MO | high | MissouriBUYS | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://missouribuys.mo.gov/ | High Medicaid systems relevance. |
| Montana | MT | medium | Montana eMACS | `vendor_platform_adapter` | opportunities, procurement_updates | https://bids.sciquest.com/apps/Router/PublicEvent?CustomerOrg=StateOfMontana | Contract history may require separate state contracts page. |
| Nebraska | NE | medium | Nebraska Materiel State Purchasing | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://das.nebraska.gov/materiel/purchasing/ | Start official purchasing site; source may be document-heavy. |
| Nevada | NV | medium | NevadaEPro | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://nevadaepro.com/ | Statewide portal should cover opportunities and contracts. |
| New Hampshire | NH | medium | New Hampshire Bids and Contracts | `html_form_adapter` | opportunities, awards, contracts, procurement_updates | https://apps.das.nh.gov/bidscontracts/bids.aspx | Likely straightforward ASP.NET table adapter. |
| New Jersey | NJ | high | NJSTART | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://www.njstart.gov/bso/ | Prior finding: bids easier; active contracts need JSF state handling. |
| New Mexico | NM | medium | New Mexico State Purchasing Division | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://www.generalservices.state.nm.us/state-purchasing/ | Prior finding: GSD/Bonfire useful for opportunities; Sunshine/CRB problematic from CLI. |
| New York | NY | high | New York State Contract Reporter | `html_search_adapter` | opportunities, procurement_updates | https://www.nyscr.ny.gov/ | High priority for opportunities. |
|  |  |  | Open Book New York | `public_portal_adapter` | contracts, awards, incumbents, expiring_contracts, open_data | https://www.openbooknewyork.com/ | Use for awarded contracts/incumbents. |
| North Carolina | NC | high | North Carolina electronic Vendor Portal | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://evp.nc.gov/ | High Medicaid/NC FAST and managed care priority. |
| North Dakota | ND | medium | North Dakota OMB State Procurement | `html_table_adapter` | opportunities, procurement_updates | https://apps.nd.gov/csd/spo/services/bidder/listCurrentSolicitations.htm | Lightweight adapter target; contract history may be separate. |
| Northern Mariana Islands | MP | low | CNMI Department of Finance Procurement | `manual_probe_required` | opportunities, procurement_updates | https://www.finance.gov.mp/ | Territory source likely HTML/PDF based. |
| Ohio | OH | high | OhioBuys | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://ohiobuys.ohio.gov/ | High Medicaid/enterprise state priority. |
| Oklahoma | OK | medium | Oklahoma OMES Solicitations | `html_table_adapter` | opportunities, awards, contracts, procurement_updates | https://oklahoma.gov/omes/services/purchasing/solicitations.html | Could be document-heavy; start with solicitations and statewide contracts. |
| Oregon | OR | high | OregonBuys | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://oregonbuys.gov/ | High Medicaid modernization relevance. |
| Pennsylvania | PA | implemented | OpenBookPA Contracts e-Library | `official_portal_json` | contracts, awards, incumbents, expiring_contracts | https://www.patreasury.gov/openbookpa/e-library/ | Implemented MVP for Gainwell/DXC/HMS/HP Enterprise contract history. |
|  |  |  | PA eMarketplace | `html_form_adapter` | opportunities, awards, procurement_updates | https://www.emarketplace.state.pa.us/ | Implemented MVP for solicitations, awards, emergency procurements. |
|  |  |  | PA DHS RHTP Funding Opportunities | `html_page_adapter` | medicaid_updates, rht_updates, grants, procurement_updates | https://www.pa.gov/agencies/dhs/programs-services/healthcare/rural-health/rhtp-funding-opportunities | Strong RHT signal source. |
| Puerto Rico | PR | medium | Puerto Rico General Services Administration | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://www.asg.pr.gov/ | Territory but material Medicaid market; include ASES procurement pages during adapter probe. |
| Rhode Island | RI | medium | Ocean State Procures | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://ridop.ri.gov/ | Small state; official portal may be document-heavy. |
| South Carolina | SC | medium | South Carolina Business Opportunities | `html_search_adapter` | opportunities, awards, procurement_updates | https://scbo.sc.gov/ | Good opportunity source; contract history may require separate procurement contracts pages. |
| South Dakota | SD | medium | South Dakota Procurement Management | `html_table_adapter` | opportunities, awards, contracts, procurement_updates | https://boa.sd.gov/central-services/procurement-management/ | Likely HTML/document adapter. |
| Tennessee | TN | high | Tennessee Edison Supplier Portal | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://supplier.edison.tn.gov/ | High Medicaid/TennCare priority. |
| Texas | TX | implemented | TXSmartBuy ESBD Solicitations | `official_portal_json` | opportunities, procurement_updates | https://www.txsmartbuy.gov/esbd | Implemented strict filter MVP; current dry run returned 0 final records. |
|  |  |  | TXSmartBuy Statewide Contracts | `official_portal_json` | contracts, awards, incumbents, expiring_contracts | https://www.txsmartbuy.gov/browsecontracts | Implemented strict filter MVP; 0 final records after filtering. |
|  |  |  | Texas DIR Cooperative Contracts | `public_portal_adapter` | contracts, awards, incumbents, expiring_contracts | https://dir.texas.gov/contracts | High-value for IT contracts; implement later with cache/throttle. |
| U.S. Virgin Islands | VI | low | Virgin Islands Department of Property and Procurement | `manual_probe_required` | opportunities, contracts, procurement_updates | https://dpp.vi.gov/procurement/ | Territory source likely HTML/PDF based. |
| Utah | UT | medium | Utah Public Procurement Place | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://purchasing.utah.gov/for-vendors/ | Start from official vendor page and public event routes. |
| Vermont | VT | medium | Vermont Business Registry and Bid System | `html_search_adapter` | opportunities, awards, procurement_updates | https://www.vermontbusinessregistry.com/BidSystem/ | Good lightweight adapter candidate. |
| Virginia | VA | high | eVA Virginia Procurement Portal | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://eva.virginia.gov/ | High Medicaid/enterprise and health IT priority. |
| Washington | WA | high | Washington Electronic Business Solution | `html_form_adapter` | opportunities, awards, contracts, procurement_updates | https://fortress.wa.gov/ga/webs/ | High Medicaid and health IT priority. |
| West Virginia | WV | medium | wvOASIS Vendor Self Service | `vendor_platform_adapter` | opportunities, awards, contracts, procurement_updates | https://www.wvoasis.gov/ | VSS-style adapter likely reusable. |
| Wisconsin | WI | high | Wisconsin VendorNet | `public_portal_adapter` | opportunities, awards, contracts, procurement_updates | https://vendornet.wi.gov/ | High Medicaid/eligibility and health IT priority. |
| Wyoming | WY | medium | Wyoming Procurement and Public Purchase | `public_portal_adapter` | opportunities, awards, procurement_updates | https://ai.wyo.gov/for-vendors | May rely on Public Purchase document/listing adapter. |

## Implementation Use

1. Pick a jurisdiction and source from `data/state_api_targets.json`.
2. Probe only that official target surface; do not broaden to random web search unless the target is dead.
3. Implement adapter under `services/state_contracts/<code>.py` or `services/state_opportunities/<code>.py`.
4. Preserve normalized outputs: `data/state_contracts.csv` and `data/state_opportunities.csv`.
5. Record unsupported/blocked portals in notes rather than forcing brittle scraping.
