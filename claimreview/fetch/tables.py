"""Destination-table shape for the claim-bundle pipeline, vendored from
claim-bundle-extraction-v2/daily_claim_bundle_pipeline.py.

Pure data: the eight Redshift tables, their column order, which extractor
function feeds each one, and how each one is reduced before loading. Kept in
its own module so pipeline.py stays readable and so a column change is a
one-file edit that can be diffed against the original pipeline script.
"""

# Analytical views included as sheets in the per-run Excel report (skipped
# gracefully if a view has not been created yet).
REPORT_VIEWS = [
    "vw_claim_overview",
    "vw_claim_treatments",
    "vw_claim_investigations",
    "vw_claim_package_amounts",
    "vw_claim_diagnosis",
    "vw_claim_care_team",
]

# Downloader statuses that still yield usable (at least payer-side) data.
EXTRACTABLE_STATUSES = {"completed", "completed_with_errors", "partially_completed"}
# Bound-parameter ceiling for a single prepared statement (Postgres protocol).
MAX_STATEMENT_PARAMS = 32000

TABLE_ORDER = [
    "claim_bundle_form_fields",
    "claim_bundle_documents",
    "claim_bundle_diagnosis",
    "claim_bundle_treatments",
    "claim_bundle_investigations",
    "claim_bundle_care_team",
    "claim_bundle_package_amounts",
    "claim_bundle_summary",
]

EXTRACTOR_FUNCTIONS = {
    "claim_bundle_form_fields": "extract_form_rows",
    "claim_bundle_documents": "extract_document_rows",
    "claim_bundle_diagnosis": "extract_diagnosis_rows",
    "claim_bundle_treatments": "extract_treatment_rows",
    "claim_bundle_investigations": "extract_investigation_rows",
    "claim_bundle_care_team": "extract_care_team_rows",
    "claim_bundle_package_amounts": "extract_package_amount_rows",
}

# Financial tables keep the snapshot-dedup behaviour (their rows differ by
# side/status and feed the side-pivoting views). Descriptive tables are collapsed
# instead, via extractor.AGGREGATION_RULES (form_fields, documents, diagnosis,
# care_team).
DEDUPLICATION_RULES = {
    "claim_bundle_treatments": {"source_json_path", "sequence_no"},
    "claim_bundle_investigations": {
        "source_json_path",
        "sequence_no",
        "item_sequence",
    },
    "claim_bundle_package_amounts": {
        "source_json_path",
        "sequence_no",
        "item_sequence",
    },
}

SUMMARY_COLUMNS = [
    "registration_id", "case_id", "provider_id", "provider_name",
    "hospital_code", "payer_id", "payer_name", "beneficiary_id", "member_id",
    "family_id", "household_id", "patient_name", "patient_dob",
    "patient_gender", "careplan_id", "admission_type", "ipop",
    "diagnosis_code", "diagnosis_name", "admission_date", "surgery_date",
    "discharge_date", "requested_amount", "approved_amount", "claimed_amount",
    "bill_amount", "claim_status", "preauth_status",
    "extraction_status", "extraction_error", "extraction_started_at",
    "extraction_completed_at", "redshift_load_status", "redshift_load_error",
    "redshift_loaded_at", "pipeline_run_id", "ingestion_date",
]

TABLE_COLUMNS = {
    "claim_bundle_summary": SUMMARY_COLUMNS,
    "claim_bundle_form_fields": [
        "registration_id", "source_side", "source_stage",
        "form_name", "group_name", "field_name", "field_value",
        "attachment_name", "source_client_s3_path",
        "pipeline_run_id", "ingestion_date",
    ],
    "claim_bundle_documents": [
        "registration_id", "source_side", "source_stage", "document_type",
        "document_desc", "mandatory_code", "file_name", "file_extension",
        "mime_type", "source_client_s3_path",
        "file_size_bytes", "file_exists_flag", "valid_file_flag",
        "document_status", "file_hash_sha256", "reference_count",
        "pipeline_run_id", "ingestion_date",
    ],
    "claim_bundle_diagnosis": [
        "registration_id", "source_side", "source_stage",
        "diagnosis_id", "diagnosis_code", "diagnosis_name", "diagnosis_type",
        "diagnosis_type_description", "pipeline_run_id", "ingestion_date",
    ],
    "claim_bundle_treatments": [
        "registration_id", "source_side", "source_stage", "sequence_no",
        "item_sequence", "item_id", "speciality_id", "treatment_type",
        "treatment_type_description", "procedure_id", "procedure_code",
        "procedure_name", "procedure_type", "program_code", "product_or_service",
        "date_on_which", "number_of_days", "quantity", "approved_quantity",
        "unit_price", "base_amount", "stratification_amount", "net_amount",
        "approved_amount", "factor", "approved_factor", "length_of_stay",
        "is_daycare", "status", "remarks", "reason_code", "procedure_category",
        "attachment_name", "source_client_s3_path",
        "pipeline_run_id", "ingestion_date",
    ],
    "claim_bundle_investigations": [
        "registration_id", "source_side", "source_stage", "source_section",
        "sequence_no", "item_sequence", "item_id", "investigation_id",
        "investigation_code", "investigation_name", "procedure_code",
        "procedure_name", "quantity", "approved_quantity", "unit_price",
        "amount", "net_amount", "approved_amount", "status", "remarks",
        "attachment_name", "source_client_s3_path", "document_adjudication",
        "pipeline_run_id", "ingestion_date",
    ],
    "claim_bundle_care_team": [
        "registration_id", "source_side", "source_stage",
        "doctor_id", "doctor_name", "doctor_registration_number",
        "doctor_qualification", "doctor_contact_number", "hpr_id",
        "pipeline_run_id", "ingestion_date",
    ],
    "claim_bundle_package_amounts": [
        "registration_id", "source_side", "source_stage", "sequence_no",
        "item_sequence", "item_id", "package_code", "package_description",
        "package_type", "liability_code", "wallet_code", "wallet_balance_amount",
        "liability_amount", "liability_approved_amount", "liabilities_json",
        "package_cost", "procedure_cost", "stratification_cost", "quantity",
        "approved_quantity", "percentage_guidelines",
        "hospital_incentive_percentage", "incentive_category", "amount",
        "net_amount", "requested_amount", "approved_amount", "claimed_amount",
        "status", "remarks", "total_deducted_amount", "reason_code",
        "pipeline_run_id", "ingestion_date",
    ],
}
