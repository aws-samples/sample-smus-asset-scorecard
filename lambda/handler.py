"""
Governance Observability Enrichment Lambda (POC)
Enriches the SMUS asset metadata export with:
  1. Form Policy Config (S3 JSON) — which forms are mandatory
  2. DataZone SearchListings — domain-wide catalog map: asset -> listing,
     plus data-product membership (data product listings carry their member listings)
  3. DataZone GetListing — present forms, glossary terms, and the latest DQ
     summary (score + rules count) per published asset
  4. DataZone ListSubscriptions — consumption/adoption metrics per listing
  5. Compliance scoring (0–100, weighted)

Why the listing APIs (and not GetAsset):
- GetAsset is the inventory API — it requires membership in the asset's OWNING
  project, so scoring N projects would require N project memberships for the
  Lambda role. Root domain unit ownership does NOT grant asset read.
- GetListing/SearchListings read the domain-wide published catalog — any domain
  user can call them with IAM permissions alone, no project membership needed.
- The listing carries everything the scorecard needs: forms, glossaryTerms, and
  latestTimeSeriesDataPointForms with the DataQualityResultFormType summary
  (passingPercentage + evaluationsCount), so ListTimeSeriesDataPoints is not
  needed either.
- Descriptions come straight from the export (business_description /
  resource_description columns).
- Trade-off: only PUBLISHED assets have listings. Unpublished inventory assets
  are scored on export-derived signals only (their listing facets stay empty).
"""

import os
import json
import uuid
import datetime as dt

import boto3
import awswrangler as wr
import pandas as pd

# ── env vars ──────────────────────────────────────────────────────────────────
REGION        = os.environ["AWS_REGION"]
DOMAIN_ID     = os.environ["DZ_DOMAIN_ID"]           # e.g. <your-poc-domain-id>
EXPORT_DB     = os.environ["EXPORT_DATABASE"]         # aws-sagemaker-catalog
EXPORT_TABLE  = os.environ["EXPORT_TABLE"]            # asset (export table name)
# SMUS asset metadata export is a federated S3 Tables catalog; it must be
# referenced with a 3-part name: "<catalog>"."<schema>"."<table>".
EXPORT_CATALOG = os.environ.get("EXPORT_CATALOG", "s3tablescatalog/aws-sagemaker-catalog")
EXPORT_SCHEMA  = os.environ.get("EXPORT_SCHEMA", "asset_metadata")
GOV_DB        = os.environ["GOV_DATABASE"]            # governance_metadata
GOV_TABLE     = os.environ["GOV_TABLE"]               # asset_compliance
# S3 Tables catalog for the governance output (s3tablescatalog/<bucket-name>)
GOV_CATALOG   = os.environ.get("GOV_CATALOG", "")
POLICY_S3_URI = os.environ["POLICY_S3_URI"]           # s3://bucket/key/form_policy.json
ATHENA_WG     = os.environ["ATHENA_WORKGROUP"]        # gov-obs
STAGING_S3    = os.environ["ATHENA_STAGING_S3"]       # s3://bucket/staging/
MIN_RULES     = int(os.environ.get("MIN_EXPECTED_RULES", "3"))
DQ_FORM       = "DataQualityResultFormType"

# Scoring weights (must sum to 1.0)
W_FORMS, W_DQ_SCORE, W_DQ_RULES, W_GLOSSARY, W_DESC = 0.50, 0.20, 0.10, 0.10, 0.10

dz = boto3.client("datazone", region_name=REGION)


# ── Step 1: Form Policy Config ─────────────────────────────────────────────────
_asset_type_forms_cache: dict[str, set] = {}  # memoize per asset type per Lambda invocation


def load_asset_type_required_forms(asset_type: str) -> set[str]:
    """Fetch which forms are natively required by an asset type in SMUS.

    Calls GetAssetType and returns the set of form type names marked
    required=True. This reflects the platform's own governance baseline —
    e.g. GlueTableForm is always required for GlueTableAssetType.

    Falls back to an empty set if the asset type is not found or the call
    fails — the S3 policy layer will still apply its overrides.
    """
    # The export's resource_type_enum (e.g. "GlueTable") is the bare suffix.
    # GetAssetType expects the fully-qualified identifier: "amazon.datazone.GlueTableAssetType".
    type_identifier = f"amazon.datazone.{asset_type}AssetType"
    if type_identifier in _asset_type_forms_cache:
        return _asset_type_forms_cache[type_identifier]

    try:
        resp = dz.get_asset_type(
            domainIdentifier=DOMAIN_ID,
            identifier=type_identifier,
        )
        result = {
            name
            for name, props in resp.get("formsOutput", {}).items()
            if props.get("required") is True
        }
    except Exception as e:
        print(f"[WARN] Could not fetch required forms for asset type {asset_type}: {e}")
        result = set()

    _asset_type_forms_cache[type_identifier] = result
    return result


def load_form_policy() -> dict:
    """
    Expected JSON structure:
    {
      "default": { "required_forms": ["DataClassification"] },
      "projects": {
        "<project_id>": {
          "GlueTable":    { "required_forms": ["DataClassification", "CriticalityForm"] },
          "S3Collection": { "required_forms": ["DataClassification", "RetentionPolicy"] }
        }
      }
    }
    """
    bucket, key = POLICY_S3_URI.replace("s3://", "").split("/", 1)
    body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def required_forms(policy: dict, project_id: str, resource_type: str) -> list[str]:
    """Return the union of SMUS-native required forms (from GetAssetType) and
    org-defined policy overrides (from S3 JSON). The asset type baseline reflects
    the platform's own governance rules; the policy layer adds project-specific
    requirements on top.
    """
    # Baseline: forms natively required by the SMUS asset type definition
    platform_required = load_asset_type_required_forms(resource_type)

    # Org overrides from S3 policy (project- or type-scoped)
    project_policy = policy.get("projects", {}).get(project_id, {})
    type_policy = project_policy.get(resource_type, {})
    if "required_forms" in type_policy:
        org_required = set(type_policy["required_forms"])
    elif "required_forms" in project_policy:
        org_required = set(project_policy["required_forms"])
    else:
        org_required = set(policy.get("default", {}).get("required_forms", []))

    # Union: both platform-native AND org-specific requirements must be met
    return sorted(platform_required | org_required)


# ── Steps 2 + 3: DataZone listing APIs (membership-free catalog reads) ────────
def build_listing_maps() -> tuple[dict, dict]:
    """One paginated SearchListings pass over the domain-wide published catalog.

    Returns two lookups:
      * asset_to_listing: {asset_id: listing_id} — join key between the export
        inventory and the catalog listing that carries the governance facets.
      * asset_to_dp: {asset_id: {"data_product_name": ...}} — data product
        listings embed their member listings, so data-product membership is
        resolved here too (no per-project Search/GetDataProduct calls).
    """
    asset_to_listing: dict[str, str] = {}
    listing_to_asset: dict[str, str] = {}
    dp_listings: list[tuple[str, list[str]]] = []

    params = {"domainIdentifier": DOMAIN_ID, "maxResults": 50}
    while True:
        resp = dz.search_listings(**params)
        for item in resp.get("items", []):
            if "assetListing" in item:
                al = item["assetListing"]
                if al.get("entityId") and al.get("listingId"):
                    asset_to_listing[al["entityId"]] = al["listingId"]
                    listing_to_asset[al["listingId"]] = al["entityId"]
            elif "dataProductListing" in item:
                dp = item["dataProductListing"]
                members = [
                    m.get("listingId")
                    for m in dp.get("items", [])
                    if m.get("listingId")
                ]
                dp_listings.append((dp.get("name", ""), members))
        next_token = resp.get("nextToken")
        if not next_token:
            break
        params["nextToken"] = next_token

    asset_to_dp: dict[str, dict] = {}
    for dp_name, member_listing_ids in dp_listings:
        for lid in member_listing_ids:
            member_asset_id = listing_to_asset.get(lid)
            if member_asset_id:
                asset_to_dp[member_asset_id] = {"data_product_name": dp_name}

    return asset_to_listing, asset_to_dp


def fetch_listing_facets(listing_id: str | None) -> dict:
    """Governance facets for a published asset, read from its catalog listing.

    GetListing response shape (item.assetListing, relevant fields):
      - forms: JSON string — keys are "FormName" or "FormName.field"
      - glossaryTerms: list[{name, shortDescription}]
      - latestTimeSeriesDataPointForms: list[{formName, contentSummary, timestamp}]
        ← includes the DataQualityResultFormType summary with passingPercentage
          AND evaluationsCount, covering both DQ dimensions in one place

    Unpublished assets have no listing — returns empty facets so the asset is
    scored on export-derived signals only.
    """
    if not listing_id:
        return {"present_forms": set(), "glossary_count": 0, "dq_summary": None}

    resp = dz.get_listing(domainIdentifier=DOMAIN_ID, identifier=listing_id)
    al = resp.get("item", {}).get("assetListing", {}) or {}

    try:
        forms = json.loads(al.get("forms") or "{}")
    except (json.JSONDecodeError, TypeError):
        forms = {}

    # Present metadata forms — keys arrive as "FormName" or "FormName.field";
    # normalise to the bare form name so they match GetAssetType's formsOutput
    # keys. Internal pseudo-entries (__DataZoneGlossaryTerms, hasAttached) are
    # not metadata forms.
    present_forms = {
        key.split(".")[0]
        for key in forms
        if not key.startswith("__") and key != "hasAttached"
    }

    # Latest DQ summary rides on the listing time-series forms
    dq_summary = next(
        (f for f in al.get("latestTimeSeriesDataPointForms", [])
         if f.get("formName") == DQ_FORM),
        None
    )

    return {
        "present_forms":  present_forms,
        "glossary_count": len(al.get("glossaryTerms") or []),
        "dq_summary":     dq_summary,
    }


def _has_text(value) -> bool:
    """True when an export string column holds real text (not NULL/NaN/empty)."""
    if value is None or (isinstance(value, float) and value != value):  # None / NaN
        return False
    return bool(str(value).strip())


# ── Step 2: DQ details from the listing summary ────────────────────────────────
def fetch_dq_details(dq_summary: dict | None) -> tuple[float, int, str | None]:
    """
    Returns (dq_score, dq_rules_count, dq_last_run_iso).

    The listing's DataQualityResultFormType summary carries both DQ dimensions
    (passingPercentage and evaluationsCount) plus the run timestamp, so no
    extra per-asset API call is needed.

    IMPORTANT: Confirm `passingPercentage` and `evaluationsCount` field names
    against a real DataQualityResultFormType payload in your domain before relying
    on them. The exact field names may vary by DQ integration (Glue DQ vs external).
    """
    if dq_summary:
        return _parse_dq_content(
            json.loads(dq_summary.get("contentSummary") or "{}"),
            dq_summary.get("timestamp"),
        )
    return (0.0, 0, None)


def _normalize_timestamp(timestamp) -> str | None:
    """Return an ISO-8601 string for a DQ timestamp.

    The value may arrive as a datetime (boto3 auto-parses timestamp shapes), an
    epoch-milliseconds int (raw form content), or an ISO string.
    """
    if timestamp is None:
        return None
    if isinstance(timestamp, dt.datetime):
        return timestamp.isoformat()
    if isinstance(timestamp, (int, float)):
        return dt.datetime.fromtimestamp(timestamp / 1000, dt.timezone.utc).isoformat()
    return str(timestamp)


def _parse_dq_content(content: dict, timestamp) -> tuple[float, int, str | None]:
    score = float(content.get("passingPercentage") or 0)
    # evaluationsCount is the total rules evaluated; fall back to len(evaluations[])
    rules = int(
        content.get("evaluationsCount")
        or len(content.get("evaluations") or [])
        or 0
    )
    last_run = _normalize_timestamp(timestamp)
    return (score, rules, last_run)


# —— Step 5: Subscription / Consumption metrics ——————————————————————————————
def fetch_subscription_count(listing_id: str | None) -> tuple[int, list[str]]:
    """Count approved subscriptions for a given listing.

    Returns (subscription_count, list_of_subscriber_project_ids).
    If listing_id is None (asset not published), returns (0, []).
    """
    if not listing_id:
        return (0, [])

    subscriber_projects = []
    params = {
        "domainIdentifier": DOMAIN_ID,
        "subscribedListingId": listing_id,
        "status": "APPROVED",
        "maxResults": 50,
    }
    try:
        while True:
            resp = dz.list_subscriptions(**params)
            for sub in resp.get("items", []):
                principal = sub.get("subscribedPrincipal", {})
                project = principal.get("project", {})
                if project.get("id"):
                    subscriber_projects.append(project["id"])
            next_token = resp.get("nextToken")
            if not next_token:
                break
            params["nextToken"] = next_token
    except Exception as e:
        print(f"[WARN] Subscription lookup failed for listing {listing_id}: {e}")

    return (len(subscriber_projects), subscriber_projects)



# ── Step 4: Compliance scoring ─────────────────────────────────────────────────


def compute_score(
    missing_count: int,
    required_count: int,
    dq_score: float,
    dq_rules_count: int,
    glossary_count: int,
    has_business_description: bool,
    has_resource_description: bool,
) -> float:
    """
    Weighted compliance score 0–100.

    Dimensions:
    - 50% form coverage: % of required forms that are present
    - 20% DQ score:      pass/fail vs dq_score threshold (scaled 0–100)
    - 10% DQ rule coverage: actual rules vs MIN_EXPECTED_RULES (capped at 1.0)
    - 10% glossary:      binary — has at least 1 glossary term
    - 10% description:   binary — has a business OR resource description

    Design note: dq_score and dq_rules_count are intentionally SEPARATE dimensions.
    An asset with 95% score from 1 rule is less well-governed than one with 95%
    across 20 rules.
    """
    form_pts   = (1 - missing_count / max(required_count, 1)) * 100 if required_count > 0 else 100.0
    dq_pts     = min(dq_score, 100.0)
    rules_pts  = min(dq_rules_count / MIN_RULES, 1.0) * 100 if MIN_RULES > 0 else (100.0 if dq_rules_count > 0 else 0.0)
    gloss_pts  = 100.0 if glossary_count >= 1 else 0.0
    desc_pts   = 100.0 if (has_business_description or has_resource_description) else 0.0

    return round(
        W_FORMS      * form_pts
        + W_DQ_SCORE * dq_pts
        + W_DQ_RULES * rules_pts
        + W_GLOSSARY * gloss_pts
        + W_DESC     * desc_pts,
        2,
    )


# ── Per-asset enrichment ───────────────────────────────────────────────────────
def get_project_names(domain_id: str) -> dict:
    """Build a {project_id: project_name} lookup via ListProjects.

    Called once per run so the enriched output can carry a human-readable
    project name alongside the opaque project ID.
    """
    project_map = {}
    paginator_params = {"domainIdentifier": domain_id, "maxResults": 50}
    while True:
        resp = dz.list_projects(**paginator_params)
        for p in resp.get("items", []):
            project_map[p["id"]] = p["name"]
        next_token = resp.get("nextToken")
        if not next_token:
            break
        paginator_params["nextToken"] = next_token
    return project_map


def enrich_asset(
    row: dict,
    policy: dict,
    project_names: dict,
    asset_to_listing: dict,
    asset_to_dp: dict,
) -> dict:
    asset_id = row["asset_id"]

    # Steps 2+3: single GetListing call per published asset (no membership needed)
    listing_id = asset_to_listing.get(asset_id)
    facets = fetch_listing_facets(listing_id)

    # Step 1: form gap
    req_forms = required_forms(policy, row["project_id"], row["resource_type_enum"])
    missing   = sorted(set(req_forms) - facets["present_forms"])

    # Step 2: DQ details from the listing summary
    dq_score, dq_rules_count, dq_last_run = fetch_dq_details(facets["dq_summary"])

    # Step 5: Subscription / consumption metrics
    subscription_count, subscriber_projects = fetch_subscription_count(listing_id)

    # Step 6: Data product membership (resolved from the SearchListings pass)
    dp_info = asset_to_dp.get(asset_id, {})
    data_product_name = dp_info.get("data_product_name", "")

    # Descriptions come straight from the export columns:
    #   * business description — curated asset-level field (portal-entered)
    #   * resource description — technical description from the source system
    has_business_description = _has_text(row.get("business_description"))
    has_resource_description = _has_text(row.get("resource_description"))

    return {
        "asset_id":                row["asset_id"],
        "asset_name":              row["asset_name"],
        "resource_type_enum":      row["resource_type_enum"],
        "project_id":              row["project_id"],
        # Human-readable project name; fall back to the ID if not resolvable.
        "project_name":            project_names.get(row["project_id"], row["project_id"]),
        "missing_required_forms":  json.dumps(missing),  # stored as JSON string for Iceberg array
        "dq_score":                dq_score,
        "dq_rules_count":          dq_rules_count,
        "dq_last_run":             dq_last_run,
        "glossary_terms_count":    facets["glossary_count"],
        "has_business_description": has_business_description,
        "has_resource_description": has_resource_description,
        "subscription_count":      subscription_count,
        "subscriber_projects":     json.dumps(subscriber_projects),
        "data_product_name":       data_product_name,
        "overall_compliance_score": compute_score(
            len(missing), len(req_forms),
            dq_score, dq_rules_count,
            facets["glossary_count"],
            has_business_description,
            has_resource_description,
        ),
    }


# ── Lambda handler ─────────────────────────────────────────────────────────────
def handler(event, context):
    snapshot_date = (event or {}).get("snapshot_date") or dt.date.today().isoformat()
    print(f"Starting enrichment for snapshot_date={snapshot_date}")

    policy = load_form_policy()

    # Build the project ID -> name lookup once for the whole run.
    project_names = get_project_names(DOMAIN_ID)

    # Step 0: read base inventory from SMUS export (Iceberg via Athena)
    # Exclude SMUS/Athena-generated transient tables (temp_table_%); they are not
    # real catalog assets and would be noise on the scorecard.
    # B608: interpolated values are catalog/schema/table identifiers from trusted
    # CDK context/env config plus a server-generated date — not user input.
    base_df = wr.athena.read_sql_query(
        sql=f"""SELECT asset_id, asset_name, resource_type_enum, extended_metadata['owningEntityId'] AS project_id, business_description, resource_description FROM "{EXPORT_CATALOG}"."{EXPORT_SCHEMA}"."{EXPORT_TABLE}" WHERE DATE(snapshot_time) = DATE('{snapshot_date}') AND asset_name NOT LIKE 'temp_table_%'""",  # nosec B608
        database=EXPORT_SCHEMA,
        workgroup=ATHENA_WG,
        ctas_approach=False,
    )
    print(f"Base inventory: {len(base_df)} assets")

    # Nothing to enrich (e.g. snapshot not yet landed) — return early so the
    # empty-DataFrame Iceberg write below doesn't fail.
    if base_df.empty:
        print(f"No assets in export for snapshot_date={snapshot_date}; nothing to enrich.")
        return {"snapshot_date": snapshot_date, "assets_processed": 0}

    # Map the export inventory to the published catalog (one SearchListings pass)
    asset_to_listing, asset_to_dp = build_listing_maps()
    published = sum(1 for a in base_df["asset_id"] if a in asset_to_listing)
    print(f"Published assets with listings: {published}/{len(base_df)}")

    # Enrich each asset
    # POC: sequential. For >~500 assets, use ThreadPoolExecutor(max_workers=5)
    enriched = [
        enrich_asset(row, policy, project_names, asset_to_listing, asset_to_dp)
        for row in base_df.to_dict("records")
    ]

    df = pd.DataFrame(enriched)
    df["snapshot_date"] = snapshot_date

    # dq_last_run is None for every asset when no DQ runs exist yet, leaving the
    # column all-null with a generic 'object' dtype that awswrangler cannot map
    # to an Athena type. Force the catalog's 'string' type so the write succeeds.
    df["dq_last_run"] = df["dq_last_run"].astype("string")

    # Python ints become pandas int64 -> Athena bigint, but the catalog defines
    # these count columns as 'int'. Cast to int32 to match and avoid a spurious
    # schema-change rejection from to_iceberg.
    df["dq_rules_count"] = df["dq_rules_count"].astype("int32")
    df["glossary_terms_count"] = df["glossary_terms_count"].astype("int32")
    df["subscription_count"] = df["subscription_count"].astype("int32")
    df["data_product_name"] = df["data_product_name"].fillna("").astype(str)

    # ── Write to Iceberg (S3 Tables or Glue Catalog) ─────────────────────────────
    #
    # Strategy:
    #   • If GOV_CATALOG is set (S3 Tables mode): use plain Athena SQL because
    #     awswrangler's to_iceberg is designed for Glue-managed Iceberg and cannot
    #     auto-create tables in the S3 Tables service-managed catalog.
    #     Steps: CREATE TABLE IF NOT EXISTS (DDL) → DELETE partition → INSERT INTO.
    #   • If GOV_CATALOG is empty (Glue mode): use wr.athena.to_iceberg as before.

    table_ref = f'"{GOV_CATALOG}"."{GOV_DB}"."{GOV_TABLE}"' if GOV_CATALOG else f'"{GOV_DB}"."{GOV_TABLE}"'

    def _run_sql(sql: str) -> None:
        wr.athena.start_query_execution(
            sql=sql,
            database=GOV_DB if not GOV_CATALOG else None,
            workgroup=ATHENA_WG,
            wait=True,
        )

    if GOV_CATALOG:
        # ── S3 Tables mode ───────────────────────────────────────────────────────
        # NOTE: The table is pre-created out-of-band via the S3 Tables API
        # (aws s3tables create-table). Athena DDL cannot CREATE TABLE against the
        # federated "s3tablescatalog/<bucket>" name — only DML (DELETE/INSERT/SELECT)
        # accepts that 3-part identifier — so there is no CREATE TABLE step here.

        # 1. Idempotency: delete today's partition (table is guaranteed to exist)
        _run_sql(f"DELETE FROM {table_ref} WHERE snapshot_date = '{snapshot_date}'")  # nosec B608

        # 3. Insert rows via inline VALUES (works for small-to-medium batches)
        #    Build INSERT from the DataFrame rows.
        def _quote(v) -> str:
            if v is None or (isinstance(v, float) and v != v):  # None / NaN
                return "NULL"
            if isinstance(v, bool):
                return "TRUE" if v else "FALSE"
            if isinstance(v, (int, float)):
                return str(v)
            return "'" + str(v).replace("'", "''") + "'"

        cols = ", ".join(df.columns)
        rows = ",\n  ".join(
            "(" + ", ".join(_quote(row[c]) for c in df.columns) + ")"
            for _, row in df.iterrows()
        )
        _run_sql(f"INSERT INTO {table_ref} ({cols})\nVALUES\n  {rows}")  # nosec B608

    else:
        # ── Glue Catalog mode (original to_iceberg path) ─────────────────────────
        iceberg_temp_path = (
            STAGING_S3.rstrip("/") + f"/iceberg_tmp/{snapshot_date}/{uuid.uuid4().hex}/"
        )
        # Idempotency: delete today's partition before appending
        try:
            _run_sql(f"DELETE FROM {table_ref} WHERE snapshot_date = '{snapshot_date}'")  # nosec B608
        except Exception as e:
            if "not found" not in str(e).lower() and "does not exist" not in str(e).lower():
                raise
        wr.athena.to_iceberg(
            df=df,
            database=GOV_DB,
            table=GOV_TABLE,
            workgroup=ATHENA_WG,
            partition_cols=["snapshot_date"],
            temp_path=iceberg_temp_path,
            keep_files=False,
            schema_evolution=True,
        )

    result = {
        "snapshot_date": snapshot_date,
        "assets_enriched": len(enriched),
        "assets_with_form_gaps": sum(1 for r in enriched if json.loads(r["missing_required_forms"])),
        "assets_with_dq": sum(1 for r in enriched if r["dq_rules_count"] > 0),
    }
    print(f"Done: {result}")
    return result
