from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

USER_AGENT = "soe-group3-cms-medicaid-datasets/0.1"
CMS_DATA_JSON_URL = "https://data.cms.gov/data.json"
CMS_DATA_API_TEMPLATE = "https://data.cms.gov/data-api/v1/dataset/{dataset_id}/data"
CMS_PROVIDER_DATASTORE_TEMPLATE = "https://data.cms.gov/provider-data/api/1/datastore/query/{dataset_id}/0"
CMS_PROVIDER_METADATA_TEMPLATE = "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/{dataset_id}"
MEDICAID_DATASTORE_TEMPLATE = "https://data.medicaid.gov/api/1/datastore/query/{dataset_id}/0"
MEDICAID_METADATA_TEMPLATE = "https://data.medicaid.gov/api/1/metastore/schemas/dataset/items/{dataset_id}"

SIGNAL_FIELDS = [
    "signal_id",
    "source_key",
    "endpoint_type",
    "dataset_id",
    "dataset_title",
    "record_type",
    "state",
    "program_focus",
    "topic_keys",
    "metric_name",
    "metric_value",
    "metric_period",
    "date_released",
    "date_modified",
    "rht_flag",
    "importance_score",
    "score_evidence_json",
    "source_url",
    "raw_json",
    "last_checked_at",
]


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    dataset_title: str
    source_key: str
    endpoint_type: str
    program_focus: str
    topic_keys: tuple[str, ...]
    why_matters: str
    rht_flag: bool = False
    metric_period: str = ""


@dataclass(frozen=True)
class DatasetRunSummary:
    dataset_id: str
    dataset_title: str
    source_key: str
    endpoint_type: str
    why_matters: str
    source_rows: int
    signals: int


@dataclass(frozen=True)
class DatasetSignalResult:
    rows: list[dict[str, str]]
    dataset_summaries: list[DatasetRunSummary]

    @property
    def counts_by_source(self) -> dict[str, int]:
        return dict(sorted(Counter(row["source_key"] for row in self.rows).items()))

    @property
    def counts_by_endpoint(self) -> dict[str, int]:
        return dict(sorted(Counter(item.endpoint_type for item in self.dataset_summaries).items()))

    @property
    def rht_signal_count(self) -> int:
        return sum(1 for row in self.rows if row.get("rht_flag") == "true")

    @property
    def cms_signal_count(self) -> int:
        return sum(1 for row in self.rows if row.get("source_key") == "cms_data")

    @property
    def medicaid_signal_count(self) -> int:
        return sum(1 for row in self.rows if row.get("source_key") == "medicaid_data")


SELECTED_DATASETS = [
    DatasetSpec(
        dataset_id="e0eba16f-ce0d-4037-96ce-2af70c718c98",
        dataset_title="ACO REACH Providers",
        source_key="cms_data",
        endpoint_type="cms_data_api",
        program_focus="provider_data",
        topic_keys=("cms", "aco_reach", "value_based_care", "provider_data", "telehealth"),
        why_matters="ACO REACH provider participation, waiver elections, and telehealth flags show CMS model demand around provider data and care coordination.",
        metric_period="2024",
    ),
    DatasetSpec(
        dataset_id="086e48c4-87a6-4be1-8823-29e8da8f225b",
        dataset_title="Provider of Services File - Internet Quality Improvement and Evaluation System",
        source_key="cms_data",
        endpoint_type="cms_data_api",
        program_focus="rural_health",
        topic_keys=("cms", "provider_data", "provider_of_services", "rural_health", "hospitals"),
        why_matters="POS rural facility counts expose provider-data maintenance and rural hospital readiness signals without downloading the 175 MB source file.",
        rht_flag=True,
        metric_period="2026-Q2",
    ),
    DatasetSpec(
        dataset_id="f6f6505c-e8b0-4d57-b258-e2b94133aaf2",
        dataset_title="Hospital Enrollments",
        source_key="cms_data",
        endpoint_type="cms_data_api",
        program_focus="provider_data",
        topic_keys=("cms", "provider_enrollment", "hospitals", "rural_health", "reh"),
        why_matters="Hospital enrollment and REH conversion flags show provider enrollment, revalidation, and rural facility transition demand.",
        rht_flag=True,
    ),
    DatasetSpec(
        dataset_id="3746498e-874d-45d8-9c69-68603cafea60",
        dataset_title="Revalidation Due Date List",
        source_key="cms_data",
        endpoint_type="cms_data_api",
        program_focus="provider_data",
        topic_keys=("cms", "provider_enrollment", "revalidation", "provider_data"),
        why_matters="Provider revalidation due-date volume is a direct signal for enrollment operations, outreach, and provider data quality work.",
    ),
    DatasetSpec(
        dataset_id="97xg-v3wv",
        dataset_title="Rural Emergency Hospital Timely and Effective Care - Hospital",
        source_key="cms_data",
        endpoint_type="cms_provider_datastore",
        program_focus="rural_health",
        topic_keys=("cms", "rural_health", "rural_emergency_hospital", "quality", "provider_data"),
        why_matters="REH quality rows identify rural emergency hospitals and measure coverage relevant to RHT readiness and quality reporting demand.",
        rht_flag=True,
    ),
    DatasetSpec(
        dataset_id="ef16c490-861a-4b1f-9e6d-f321abdcaab1",
        dataset_title="2024 Managed Care Programs By State",
        source_key="medicaid_data",
        endpoint_type="medicaid_datastore",
        program_focus="managed_care",
        topic_keys=("medicaid", "managed_care", "1115", "waiver", "mltss", "quality"),
        why_matters="State managed-care program counts, 1115 authorities, MLTSS, and quality requirements point to plan oversight and encounter-data demand.",
        metric_period="2024",
    ),
    DatasetSpec(
        dataset_id="6165f45b-ca93-5bb5-9d06-db29c692a360",
        dataset_title="State Medicaid and CHIP Applications, Eligibility Determinations, and Enrollment Data",
        source_key="medicaid_data",
        endpoint_type="medicaid_datastore",
        program_focus="eligibility",
        topic_keys=("medicaid", "chip", "enrollment", "eligibility", "determinations"),
        why_matters="Enrollment, applications, and eligibility determinations by state reveal eligibility-system workload and contact-center demand.",
    ),
    DatasetSpec(
        dataset_id="5abea2e0-3f8e-4b49-a50d-d63d5fd9103c",
        dataset_title="State Medicaid and CHIP Eligibility Processing Data",
        source_key="medicaid_data",
        endpoint_type="medicaid_datastore",
        program_focus="eligibility",
        topic_keys=("medicaid", "chip", "renewals", "eligibility", "unwinding"),
        why_matters="Renewals due, pending renewals, and procedural disenrollments expose state eligibility processing pressure.",
    ),
    DatasetSpec(
        dataset_id="4d4eaf55-33d3-4468-80b4-63553f4530ae",
        dataset_title="Section 1915(c) waiver program participants",
        source_key="medicaid_data",
        endpoint_type="medicaid_datastore",
        program_focus="medicaid",
        topic_keys=("medicaid", "1915c", "waiver", "hcbs", "service_use"),
        why_matters="1915(c) waiver participation shows HCBS program scale and waiver administration demand.",
    ),
    DatasetSpec(
        dataset_id="93b36a8e-4dd5-4ff4-9a8b-8c6537684705",
        dataset_title="Dual Status Information for Medicaid and CHIP Beneficiaries by Year",
        source_key="medicaid_data",
        endpoint_type="medicaid_datastore",
        program_focus="medicare",
        topic_keys=("medicaid", "medicare", "dual_enrollment", "care_coordination"),
        why_matters="Full-dual enrollment identifies Medicare-Medicaid coordination demand and states with high integrated-care opportunity.",
    ),
    DatasetSpec(
        dataset_id="8062e2f4-4c0a-41c9-8217-979468a80986",
        dataset_title="Medicaid and CHIP enrollees who received mental health or SUD services",
        source_key="medicaid_data",
        endpoint_type="medicaid_datastore",
        program_focus="quality",
        topic_keys=("medicaid", "behavioral_health", "service_use", "quality", "rural_health"),
        why_matters="Behavioral health and SUD service-use rates, including rural residence, support RHT and quality scoring.",
        rht_flag=True,
    ),
]


class CmsMedicaidDatasetClient:
    def __init__(self, timeout: int = 60, retries: int = 3) -> None:
        self.timeout = timeout
        self.retries = retries
        self._cms_catalog: dict[str, dict[str, Any]] | None = None

    def get_json(self, url: str) -> Any:
        if "api.sam.gov" in url.lower():
            raise RuntimeError("Refusing to call api.sam.gov from CMS/Medicaid dataset client")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(1 + attempt)
        raise RuntimeError(f"Dataset API request failed for {url}: {last_error}")

    def cms_metadata(self, dataset_id: str) -> dict[str, Any]:
        if self._cms_catalog is None:
            payload = self.get_json(CMS_DATA_JSON_URL)
            self._cms_catalog = {}
            for row in payload.get("dataset", []) if isinstance(payload, dict) else []:
                text = json.dumps(row)
                if dataset_id_from_cms_row(row):
                    self._cms_catalog[dataset_id_from_cms_row(row)] = row
                elif dataset_id in text:
                    self._cms_catalog[dataset_id] = row
        return dict(self._cms_catalog.get(dataset_id, {}))

    def cms_provider_metadata(self, dataset_id: str) -> dict[str, Any]:
        return ensure_dict(self.get_json(CMS_PROVIDER_METADATA_TEMPLATE.format(dataset_id=dataset_id)))

    def medicaid_metadata(self, dataset_id: str) -> dict[str, Any]:
        return ensure_dict(self.get_json(MEDICAID_METADATA_TEMPLATE.format(dataset_id=dataset_id)))

    def metadata_for(self, spec: DatasetSpec) -> dict[str, Any]:
        if spec.endpoint_type == "cms_data_api":
            return self.cms_metadata(spec.dataset_id)
        if spec.endpoint_type == "cms_provider_datastore":
            return self.cms_provider_metadata(spec.dataset_id)
        if spec.endpoint_type == "medicaid_datastore":
            return self.medicaid_metadata(spec.dataset_id)
        return {}

    def cms_data_stats(self, dataset_id: str, filters: dict[str, str] | None = None) -> dict[str, Any]:
        base = CMS_DATA_API_TEMPLATE.format(dataset_id=dataset_id) + "/stats"
        return ensure_dict(self.get_json(add_query(base, cms_filter_params(filters))))

    def cms_data_rows(
        self,
        dataset_id: str,
        size: int = 1000,
        offset: int = 0,
        filters: dict[str, str] | None = None,
        columns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {"size": str(size), "offset": str(offset)}
        params.update(cms_filter_params(filters))
        if columns:
            params["column"] = ",".join(columns)
        payload = self.get_json(add_query(CMS_DATA_API_TEMPLATE.format(dataset_id=dataset_id), params))
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def cms_provider_datastore(self, dataset_id: str, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
        url = add_query(
            CMS_PROVIDER_DATASTORE_TEMPLATE.format(dataset_id=dataset_id),
            {"limit": str(limit), "offset": str(offset)},
        )
        return ensure_dict(self.get_json(url))

    def medicaid_datastore(self, dataset_id: str, limit: int = 5000, offset: int = 0) -> dict[str, Any]:
        url = add_query(
            MEDICAID_DATASTORE_TEMPLATE.format(dataset_id=dataset_id),
            {"limit": str(limit), "offset": str(offset)},
        )
        return ensure_dict(self.get_json(url))

    def all_datastore_rows(
        self,
        spec: DatasetSpec,
        page_size: int = 5000,
        max_rows: int = 15000,
    ) -> tuple[list[dict[str, Any]], int]:
        fetcher = self.cms_provider_datastore if spec.endpoint_type == "cms_provider_datastore" else self.medicaid_datastore
        first = fetcher(spec.dataset_id, limit=min(page_size, max_rows), offset=0)
        rows = result_rows(first)
        total = int_value(first.get("count"), default=len(rows))
        while len(rows) < min(total, max_rows):
            payload = fetcher(spec.dataset_id, limit=min(page_size, max_rows - len(rows)), offset=len(rows))
            page = result_rows(payload)
            if not page:
                break
            rows.extend(page)
        return rows, total


def build_selected_dataset_signals(
    client: CmsMedicaidDatasetClient | None = None,
    checked_at: str | None = None,
    max_source_rows: int = 15000,
    progress: Callable[[str], None] | None = None,
) -> DatasetSignalResult:
    client = client or CmsMedicaidDatasetClient()
    checked_at = checked_at or utc_now()
    all_rows: list[dict[str, str]] = []
    summaries: list[DatasetRunSummary] = []

    for spec in SELECTED_DATASETS:
        emit(progress, f"pulling {spec.dataset_id} {spec.dataset_title}")
        metadata = client.metadata_for(spec)
        before = len(all_rows)
        source_rows = 0
        if spec.endpoint_type == "cms_data_api":
            built_rows, source_rows = cms_data_api_signals(client, spec, metadata, checked_at)
        elif spec.endpoint_type == "cms_provider_datastore":
            rows, source_rows = client.all_datastore_rows(spec, page_size=1000, max_rows=max_source_rows)
            built_rows = cms_provider_datastore_signals(spec, metadata, rows, source_rows, checked_at)
        elif spec.endpoint_type == "medicaid_datastore":
            rows, source_rows = client.all_datastore_rows(spec, page_size=5000, max_rows=max_source_rows)
            built_rows = medicaid_datastore_signals(spec, metadata, rows, source_rows, checked_at)
        else:
            built_rows = []
        all_rows.extend(built_rows)
        summaries.append(
            DatasetRunSummary(
                dataset_id=spec.dataset_id,
                dataset_title=title_for(spec, metadata),
                source_key=spec.source_key,
                endpoint_type=spec.endpoint_type,
                why_matters=spec.why_matters,
                source_rows=source_rows,
                signals=len(all_rows) - before,
            )
        )

    all_rows.sort(key=lambda row: (row["source_key"], row["dataset_title"], row["state"], row["metric_name"]))
    return DatasetSignalResult(rows=all_rows, dataset_summaries=summaries)


def cms_data_api_signals(
    client: CmsMedicaidDatasetClient,
    spec: DatasetSpec,
    metadata: dict[str, Any],
    checked_at: str,
) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    total_stats = client.cms_data_stats(spec.dataset_id)
    total_rows = int_value(total_stats.get("total_rows") or total_stats.get("found_rows"))

    if spec.dataset_id == "e0eba16f-ce0d-4037-96ce-2af70c718c98":
        add_metric(rows, spec, metadata, checked_at, "aco_reach_provider_rows", total_rows, raw={"stats": total_stats})
        telehealth = client.cms_data_stats(spec.dataset_id, {"telehealth_waiver": "Y"})
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "aco_reach_telehealth_waiver_provider_rows",
            int_value(telehealth.get("found_rows")),
            rht_flag=True,
            raw={"filters": {"telehealth_waiver": "Y"}, "stats": telehealth},
        )
    elif spec.dataset_id == "086e48c4-87a6-4be1-8823-29e8da8f225b":
        add_metric(rows, spec, metadata, checked_at, "provider_of_services_rows", total_rows, rht_flag=False, raw={"stats": total_stats})
        rural = client.cms_data_stats(spec.dataset_id, {"cbsa_urbn_rrl_ind": "R"})
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "provider_of_services_rural_rows",
            int_value(rural.get("found_rows")),
            rht_flag=True,
            raw={"filters": {"cbsa_urbn_rrl_ind": "R"}, "stats": rural},
        )
    elif spec.dataset_id == "f6f6505c-e8b0-4d57-b258-e2b94133aaf2":
        add_metric(rows, spec, metadata, checked_at, "hospital_enrollment_rows", total_rows, rht_flag=False, raw={"stats": total_stats})
        reh = client.cms_data_stats(spec.dataset_id, {"REH CONVERSION FLAG": "Y"})
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "hospital_reh_conversion_rows",
            int_value(reh.get("found_rows")),
            rht_flag=True,
            raw={"filters": {"REH CONVERSION FLAG": "Y"}, "stats": reh},
        )
        swing_bed = client.cms_data_stats(spec.dataset_id, {"SUBGROUP - SWING-BED APPROVED": "Y"})
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "hospital_swing_bed_approved_rows",
            int_value(swing_bed.get("found_rows")),
            rht_flag=True,
            raw={"filters": {"SUBGROUP - SWING-BED APPROVED": "Y"}, "stats": swing_bed},
        )
    elif spec.dataset_id == "3746498e-874d-45d8-9c69-68603cafea60":
        add_metric(rows, spec, metadata, checked_at, "provider_revalidation_due_list_rows", total_rows, raw={"stats": total_stats})
    else:
        add_metric(rows, spec, metadata, checked_at, "dataset_rows", total_rows, raw={"stats": total_stats})

    return rows, total_rows


def cms_provider_datastore_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    add_metric(
        rows,
        spec,
        metadata,
        checked_at,
        "rural_emergency_hospital_quality_measure_rows",
        source_row_count,
        rht_flag=True,
        raw={"aggregation": "datastore count", "source_rows_used": len(source_rows), "source_row_count": source_row_count},
    )
    facilities_by_state: dict[str, set[str]] = defaultdict(set)
    measure_rows_by_state: Counter[str] = Counter()
    periods_by_state: dict[str, set[str]] = defaultdict(set)
    for row in source_rows:
        state = clean(row.get("state"))
        if not state:
            continue
        facility_id = clean(row.get("facility_id") or row.get("facility_name"))
        if facility_id:
            facilities_by_state[state].add(facility_id)
        measure_rows_by_state[state] += 1
        period = date_range(row.get("start_date"), row.get("end_date"))
        if period:
            periods_by_state[state].add(period)
    for state in sorted(facilities_by_state):
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "rural_emergency_hospital_facilities",
            len(facilities_by_state[state]),
            state=state,
            metric_period=latest_text(periods_by_state[state]),
            rht_flag=True,
            raw={
                "aggregation": "unique facility_id by state",
                "measure_rows": measure_rows_by_state[state],
                "source_rows_used": len(source_rows),
            },
        )
    return rows


def medicaid_datastore_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    if spec.dataset_id == "ef16c490-861a-4b1f-9e6d-f321abdcaab1":
        return managed_care_signals(spec, metadata, source_rows, source_row_count, checked_at)
    if spec.dataset_id == "6165f45b-ca93-5bb5-9d06-db29c692a360":
        return enrollment_determination_signals(spec, metadata, source_rows, source_row_count, checked_at)
    if spec.dataset_id == "5abea2e0-3f8e-4b49-a50d-d63d5fd9103c":
        return eligibility_processing_signals(spec, metadata, source_rows, source_row_count, checked_at)
    if spec.dataset_id == "4d4eaf55-33d3-4468-80b4-63553f4530ae":
        return waiver_1915c_signals(spec, metadata, source_rows, source_row_count, checked_at)
    if spec.dataset_id == "93b36a8e-4dd5-4ff4-9a8b-8c6537684705":
        return dual_status_signals(spec, metadata, source_rows, source_row_count, checked_at)
    if spec.dataset_id == "8062e2f4-4c0a-41c9-8217-979468a80986":
        return behavioral_service_use_signals(spec, metadata, source_rows, source_row_count, checked_at)
    return []


def managed_care_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    by_state: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        state = clean(row.get("state"))
        if state:
            by_state[state].append(row)
    for state in sorted(by_state):
        state_rows = by_state[state]
        authority_1115 = sum(1 for row in state_rows if "1115" in clean(row.get("federal_operating_authority")))
        mltss = sum(1 for row in state_rows if "MLTSS" in clean(row.get("program_type")))
        hedis = sum(1 for row in state_rows if yesish(row.get("quality_assurance_and_improvement_hedis_data_required")))
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "managed_care_programs_2024",
            len(state_rows),
            state=state,
            raw={
                "aggregation": "program count by state",
                "1115_authority_programs": authority_1115,
                "mltss_programs": mltss,
                "hedis_required_programs": hedis,
                "source_rows_used": len(source_rows),
                "source_row_count": source_row_count,
            },
        )
        if authority_1115:
            add_metric(
                rows,
                spec,
                metadata,
                checked_at,
                "managed_care_1115_authority_programs",
                authority_1115,
                state=state,
                raw={"aggregation": "programs with federal_operating_authority containing 1115", "source_rows_used": len(source_rows)},
            )
        if mltss:
            add_metric(
                rows,
                spec,
                metadata,
                checked_at,
                "managed_long_term_services_supports_programs",
                mltss,
                state=state,
                raw={"aggregation": "program_type contains MLTSS", "source_rows_used": len(source_rows)},
            )
    return rows


def enrollment_determination_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    metrics = [
        ("total_medicaid_and_chip_enrollment", "total_medicaid_and_chip_enrollment"),
        ("total_medicaid_and_chip_determinations", "total_medicaid_and_chip_determinations"),
        ("new_applications_submitted", "new_applications_submitted_to_medicaid_and_chip_agencies"),
    ]
    return reporting_period_metric_signals(spec, metadata, source_rows, source_row_count, checked_at, metrics)


def eligibility_processing_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    metrics = [
        ("beneficiaries_with_renewal_due", "beneficiaries_with_a_renewal_due"),
        ("beneficiaries_renewed_total", "beneficiaries_whose_coverage_was_renewed_total"),
        ("procedural_disenrollments_at_renewal", "beneficiaries_disenrolled_for_procedural_reasons_at_renewal"),
        ("pending_renewals", "beneficiaries_with_a_pending_renewal"),
    ]
    return reporting_period_metric_signals(spec, metadata, source_rows, source_row_count, checked_at, metrics)


def reporting_period_metric_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
    metrics: list[tuple[str, str]],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    selected = latest_reporting_row_by_state(source_rows)
    for state in sorted(selected):
        row = selected[state]
        period = clean(row.get("reporting_period"))
        for metric_name, field in metrics:
            value = numeric_text(row.get(field))
            if value == "":
                continue
            add_metric(
                rows,
                spec,
                metadata,
                checked_at,
                metric_name,
                value,
                state=state,
                metric_period=period,
                raw={
                    "aggregation": "latest reporting_period by state",
                    "field": field,
                    "state_name": clean(row.get("state_name")),
                    "source_rows_used": len(source_rows),
                    "source_row_count": source_row_count,
                },
            )
    return rows


def waiver_1915c_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    year = latest_field(source_rows, "year")
    for row in source_rows:
        if clean(row.get("year")) != year or clean(row.get("category")) != "Enrolled in 1915(c) waiver":
            continue
        geography = clean(row.get("geography"))
        value = numeric_text(row.get("count_of_enrollees"))
        if not geography or value == "":
            continue
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "section_1915c_waiver_enrollees",
            value,
            state="" if geography == "National" else geography,
            metric_period=year,
            raw={
                "aggregation": "latest year category='Enrolled in 1915(c) waiver'",
                "geography": geography,
                "subpopulation_topic": clean(row.get("subpopulation_topic")),
                "subpopulation": clean(row.get("subpopulation")),
                "percentage_of_enrollees": clean(row.get("percentage_of_enrollees")),
                "denominator_count_of_enrollees": clean(row.get("denominator_count_of_enrollees")),
                "source_rows_used": len(source_rows),
                "source_row_count": source_row_count,
            },
        )
    return rows


def dual_status_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    year = latest_field(source_rows, "year")
    for row in source_rows:
        if clean(row.get("year")) != year or clean(row.get("dualstatus")) != "Full dual eligibility":
            continue
        state = clean(row.get("state"))
        value = numeric_text(row.get("averageenrollmentpermonth") or row.get("counteverenrolled"))
        if not state or value == "":
            continue
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "full_dual_average_monthly_enrollment",
            value,
            state=state,
            metric_period=year,
            raw={
                "aggregation": "latest year full dual eligibility",
                "count_ever_enrolled": clean(row.get("counteverenrolled")),
                "count_last_month_enrollment": clean(row.get("countlastmonthenrollment")),
                "source_rows_used": len(source_rows),
                "source_row_count": source_row_count,
            },
        )
    return rows


def behavioral_service_use_signals(
    spec: DatasetSpec,
    metadata: dict[str, Any],
    source_rows: list[dict[str, Any]],
    source_row_count: int,
    checked_at: str,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    year = latest_field(source_rows, "year")
    for row in source_rows:
        if clean(row.get("year")) != year:
            continue
        if clean(row.get("subpopulation_topic")) != "Urban or rural residence":
            continue
        if clean(row.get("category")) != "Received MH or SUD services":
            continue
        subpopulation = clean(row.get("subpopulation"))
        value = numeric_text(row.get("percentage_of_enrollees"))
        if not subpopulation or value == "":
            continue
        add_metric(
            rows,
            spec,
            metadata,
            checked_at,
            "mh_sud_service_use_percentage",
            value,
            metric_period=year,
            rht_flag=subpopulation == "Rural",
            raw={
                "aggregation": "latest year urban/rural residence percentage",
                "subpopulation": subpopulation,
                "count_of_enrollees": clean(row.get("count_of_enrollees")),
                "denominator_count_of_enrollees": clean(row.get("denominator_count_of_enrollees")),
                "source_rows_used": len(source_rows),
                "source_row_count": source_row_count,
            },
        )
    return rows


def add_metric(
    rows: list[dict[str, str]],
    spec: DatasetSpec,
    metadata: dict[str, Any],
    checked_at: str,
    metric_name: str,
    metric_value: Any,
    state: str = "",
    metric_period: str = "",
    rht_flag: bool | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    metric_value_text = format_metric(metric_value)
    if metric_value_text == "":
        return
    metric_period = metric_period or spec.metric_period
    title = title_for(spec, metadata)
    rht = spec.rht_flag if rht_flag is None else rht_flag
    dimension = signal_dimension(raw)
    evidence = {
        "dataset_id": spec.dataset_id,
        "endpoint_type": spec.endpoint_type,
        "why_matters": spec.why_matters,
        "topic_keys": list(spec.topic_keys),
        "metric_name": metric_name,
        "metric_value": metric_value_text,
        "metric_period": metric_period,
        "state": state,
        "signal_dimension": dimension,
        "rht_flag": rht,
        "method": (raw or {}).get("aggregation", "API/stat query"),
    }
    rows.append(
        {
            "signal_id": signal_id(spec.source_key, spec.dataset_id, metric_name, state, metric_period, dimension),
            "source_key": spec.source_key,
            "endpoint_type": spec.endpoint_type,
            "dataset_id": spec.dataset_id,
            "dataset_title": title,
            "record_type": "dataset_signal",
            "state": state,
            "program_focus": spec.program_focus,
            "topic_keys": "; ".join(spec.topic_keys),
            "metric_name": metric_name,
            "metric_value": metric_value_text,
            "metric_period": metric_period,
            "date_released": metadata_date(metadata, "released", "issued"),
            "date_modified": metadata_date(metadata, "modified"),
            "rht_flag": "true" if rht else "false",
            "importance_score": str(importance_score(spec, metric_name, rht)),
            "score_evidence_json": compact_json(evidence),
            "source_url": source_url_for(spec, metadata),
            "raw_json": compact_json({"why_matters": spec.why_matters, **(raw or {})}),
            "last_checked_at": checked_at,
        }
    )


def signal_dimension(raw: dict[str, Any] | None) -> str:
    raw = raw or {}
    values: list[str] = []
    for key in ("signal_key", "geography", "subpopulation_topic", "subpopulation", "state_name", "field", "category"):
        value = clean(raw.get(key))
        if value and value.upper() not in {"N/A", "NA"} and value not in values:
            values.append(value)
    return "|".join(values)


def latest_reporting_row_by_state(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        state = clean(row.get("state_abbreviation") or row.get("state"))
        period = clean(row.get("reporting_period"))
        if not state or not period:
            continue
        current = selected.get(state)
        if current is None or reporting_sort_key(row) > reporting_sort_key(current):
            selected[state] = row
    return selected


def reporting_sort_key(row: dict[str, Any]) -> tuple[str, int, int]:
    return (
        clean(row.get("reporting_period")),
        1 if clean(row.get("final_report")).upper() == "Y" else 0,
        1 if clean(row.get("preliminary_or_updated") or row.get("original_or_updated")).upper() in {"U", "O"} else 0,
    )


def latest_field(rows: list[dict[str, Any]], field: str) -> str:
    values = [clean(row.get(field)) for row in rows if clean(row.get(field))]
    return max(values) if values else ""


def latest_text(values: set[str]) -> str:
    return max(values) if values else ""


def title_for(spec: DatasetSpec, metadata: dict[str, Any]) -> str:
    return clean(metadata.get("title")) or spec.dataset_title


def source_url_for(spec: DatasetSpec, metadata: dict[str, Any]) -> str:
    landing_page = clean(metadata.get("landingPage"))
    if landing_page:
        return landing_page
    if spec.endpoint_type == "cms_data_api":
        return CMS_DATA_API_TEMPLATE.format(dataset_id=spec.dataset_id)
    if spec.endpoint_type == "cms_provider_datastore":
        return CMS_PROVIDER_DATASTORE_TEMPLATE.format(dataset_id=spec.dataset_id)
    return MEDICAID_DATASTORE_TEMPLATE.format(dataset_id=spec.dataset_id)


def metadata_date(metadata: dict[str, Any], *fields: str) -> str:
    for field in fields:
        parsed = iso_date(metadata.get(field))
        if parsed:
            return parsed
    return ""


def dataset_id_from_cms_row(row: dict[str, Any]) -> str:
    for distribution in row.get("distribution") or []:
        if not isinstance(distribution, dict):
            continue
        access_url = clean(distribution.get("accessURL"))
        marker = "/dataset/"
        if marker in access_url and access_url.endswith("/data"):
            return access_url.split(marker, 1)[1].split("/", 1)[0]
    identifier = clean(row.get("identifier"))
    if "/dataset/" in identifier:
        return identifier.split("/dataset/", 1)[1].split("/", 1)[0]
    return ""


def date_range(start: Any, end: Any) -> str:
    start_date = iso_date(start)
    end_date = iso_date(end)
    if start_date and end_date:
        return f"{start_date}/{end_date}"
    return start_date or end_date


def iso_date(value: Any) -> str:
    text = clean(value).replace("Z", "+00:00")
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return dt.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return ""


def add_query(url: str, params: dict[str, str] | None = None) -> str:
    params = {key: value for key, value in (params or {}).items() if value != ""}
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return url + separator + urllib.parse.urlencode(params)


def cms_filter_params(filters: dict[str, str] | None) -> dict[str, str]:
    return {f"filter[{key}]": value for key, value in (filters or {}).items()}


def result_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("results")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def ensure_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def int_value(value: Any, default: int = 0) -> int:
    number = parse_number(value)
    return int(number) if number is not None else default


def parse_number(value: Any) -> float | None:
    text = clean(value)
    if not text or text.lower() in {"not available", "n/a", "null", "none", "-"}:
        return None
    text = text.replace(",", "").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def numeric_text(value: Any) -> str:
    number = parse_number(value)
    return format_metric(number) if number is not None else ""


def format_metric(value: Any) -> str:
    number = parse_number(value)
    if number is None:
        return clean(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def yesish(value: Any) -> bool:
    return clean(value).lower() in {"y", "yes", "true", "1", "required"}


def clean(value: Any) -> str:
    if value is None:
        return ""
    return html.unescape(str(value)).replace("\r", " ").replace("\n", " ").strip()


def signal_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(clean(part) for part in parts).encode("utf-8")).hexdigest()[:16]
    return f"cmsmedicaid-{digest}"


def compact_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def importance_score(spec: DatasetSpec, metric_name: str, rht_flag: bool) -> int:
    score = 45
    text = " ".join((spec.program_focus, " ".join(spec.topic_keys), metric_name)).lower()
    weights = {
        "eligibility": 15,
        "enrollment": 12,
        "managed_care": 14,
        "1115": 10,
        "1915c": 9,
        "dual": 10,
        "provider_enrollment": 13,
        "revalidation": 13,
        "provider_data": 10,
        "quality": 8,
        "behavioral_health": 8,
        "telehealth": 7,
        "rural_health": 10,
        "aco_reach": 9,
    }
    for term, weight in weights.items():
        if term in text:
            score += weight
    if rht_flag:
        score += 12
    return min(score, 95)


def emit(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)


def write_signals(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
