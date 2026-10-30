-- ============================================================
-- 0. One-time: create governance table (run before first Lambda execution)
--
-- NOTE: snapshot_date is stored as VARCHAR (string 'YYYY-MM-DD').
-- When filtering by current date, use:
--   WHERE snapshot_date = date_format(current_date, '%Y-%m-%d')
-- ============================================================
CREATE TABLE governance_metadata.asset_compliance (
  asset_id                  string,
  asset_name                string,
  resource_type_enum        string,
  project_id                string,
  project_name              string,
  missing_required_forms    string,   -- JSON array stored as string
  dq_score                  double,
  dq_rules_count            int,
  dq_last_run               string,
  glossary_terms_count      int,
  subscription_count        int,
  subscriber_projects       string,   -- JSON array of project IDs consuming this asset
  overall_compliance_score  double,
  data_product_name         string,   -- name of the data product this asset belongs to (empty if none)
  snapshot_date             string
)
PARTITIONED BY (snapshot_date)
TBLPROPERTIES ('table_type' = 'ICEBERG');


-- ============================================================
-- 1. Compliance scorecard per project (latest snapshot)
-- ============================================================
WITH latest AS (
  SELECT MAX(snapshot_date) AS d FROM governance_metadata.asset_compliance
)
SELECT
  c.project_id,
  c.project_name,
  COUNT(*)                                                                AS total_assets,
  ROUND(AVG(c.overall_compliance_score), 1)                              AS avg_compliance_score,
  -- Form coverage
  COUNT(CASE WHEN JSON_ARRAY_LENGTH(c.missing_required_forms) > 0 THEN 1 END) AS assets_with_form_gaps,
  -- Data quality score
  COUNT(CASE WHEN c.dq_score >= 80 THEN 1 END)                          AS assets_passing_dq,
  -- DQ rule coverage
  COUNT(CASE WHEN c.dq_rules_count >= 3 THEN 1 END)                     AS assets_with_sufficient_dq_rules,
  ROUND(AVG(c.dq_rules_count), 1)                                        AS avg_dq_rules_per_asset,
  -- Glossary
  COUNT(CASE WHEN c.glossary_terms_count > 0 THEN 1 END)                AS assets_with_glossary,
  -- Compliance banding
  COUNT(CASE WHEN c.overall_compliance_score >= 80 THEN 1 END)          AS compliant_80plus,
  COUNT(CASE WHEN c.overall_compliance_score BETWEEN 50 AND 79 THEN 1 END) AS at_risk_50_79,
  COUNT(CASE WHEN c.overall_compliance_score < 50 THEN 1 END)           AS critical_lt_50
FROM governance_metadata.asset_compliance c
JOIN latest ON c.snapshot_date = latest.d
GROUP BY c.project_id, c.project_name
ORDER BY avg_compliance_score ASC;


-- ============================================================
-- 2. Non-compliant assets — exception list (latest snapshot)
-- ============================================================
WITH latest AS (
  SELECT MAX(snapshot_date) AS d FROM governance_metadata.asset_compliance
)
SELECT
  c.asset_id,
  c.asset_name,
  c.resource_type_enum,
  c.project_id,
  c.missing_required_forms,
  c.dq_score,
  c.dq_rules_count,
  c.glossary_terms_count,
  c.overall_compliance_score
FROM governance_metadata.asset_compliance c
JOIN latest ON c.snapshot_date = latest.d
WHERE JSON_ARRAY_LENGTH(c.missing_required_forms) > 0
   OR c.dq_rules_count < 3
   OR c.glossary_terms_count = 0
ORDER BY c.overall_compliance_score ASC;


-- ============================================================
-- 3. Compliance trend since rule publication (time travel)
-- ============================================================
SELECT
  snapshot_date,
  COUNT(*)                                                      AS total_assets,
  ROUND(AVG(overall_compliance_score), 1)                       AS avg_compliance_score,
  ROUND(AVG(dq_rules_count), 1)                                 AS avg_dq_rules,
  COUNT(CASE WHEN JSON_ARRAY_LENGTH(missing_required_forms) = 0 THEN 1 END) AS fully_compliant_forms
FROM governance_metadata.asset_compliance
WHERE snapshot_date >= '2026-05-01'   -- replace with rule publish date
GROUP BY snapshot_date
ORDER BY snapshot_date;


-- ============================================================
-- 4. Assets that became compliant (forms) in last 7 days
-- ============================================================
SELECT
  t.asset_id,
  t.asset_name,
  t.project_id,
  p.missing_required_forms AS forms_missing_last_week,
  t.missing_required_forms AS forms_missing_today
FROM governance_metadata.asset_compliance t
JOIN governance_metadata.asset_compliance p
  ON t.asset_id = p.asset_id
WHERE t.snapshot_date = (SELECT MAX(snapshot_date) FROM governance_metadata.asset_compliance)
  AND p.snapshot_date = DATE_FORMAT(DATE_ADD('day', -7,
        DATE_PARSE((SELECT MAX(snapshot_date) FROM governance_metadata.asset_compliance), '%Y-%m-%d')), '%Y-%m-%d')
  AND JSON_ARRAY_LENGTH(p.missing_required_forms) > 0
  AND JSON_ARRAY_LENGTH(t.missing_required_forms) = 0;


-- ============================================================
-- 5. Spot check — 25 worst-scored assets (validation query)
-- ============================================================
SELECT
  asset_id, asset_name, resource_type_enum, project_id, project_name,
  missing_required_forms, dq_score, dq_rules_count, dq_last_run,
  glossary_terms_count, overall_compliance_score, snapshot_date
FROM governance_metadata.asset_compliance
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM governance_metadata.asset_compliance)
ORDER BY overall_compliance_score ASC
LIMIT 25;


-- ============================================================
-- 6. Most consumed assets (subscription hotspots)
-- ============================================================
WITH latest AS (
  SELECT MAX(snapshot_date) AS d FROM governance_metadata.asset_compliance
)
SELECT
  c.asset_name,
  c.project_name,
  c.subscription_count,
  c.overall_compliance_score,
  CASE
    WHEN c.subscription_count >= 2 AND c.overall_compliance_score < 50 THEN '🔴 HIGH RISK'
    WHEN c.subscription_count >= 1 AND c.overall_compliance_score < 50 THEN '🟡 MEDIUM RISK'
    ELSE '🟢 OK'
  END AS risk_level
FROM governance_metadata.asset_compliance c
JOIN latest ON c.snapshot_date = latest.d
WHERE c.subscription_count > 0
ORDER BY c.subscription_count DESC, c.overall_compliance_score ASC;


-- ============================================================
-- 7. Projects producing most-consumed data (publisher scorecard)
-- ============================================================
WITH latest AS (
  SELECT MAX(snapshot_date) AS d FROM governance_metadata.asset_compliance
)
SELECT
  c.project_name,
  COUNT(*) AS total_assets,
  SUM(c.subscription_count) AS total_subscriptions,
  ROUND(AVG(c.overall_compliance_score), 1) AS avg_compliance,
  COUNT(CASE WHEN c.subscription_count > 0 THEN 1 END) AS assets_with_subscribers
FROM governance_metadata.asset_compliance c
JOIN latest ON c.snapshot_date = latest.d
GROUP BY c.project_name
ORDER BY total_subscriptions DESC;


-- ============================================================
-- 8. Data product compliance scorecard
-- ============================================================
WITH latest AS (
  SELECT MAX(snapshot_date) AS d FROM governance_metadata.asset_compliance
)
SELECT
  COALESCE(NULLIF(c.data_product_name, ''), '(no data product)') AS data_product,
  c.project_name,
  COUNT(*) AS total_assets,
  ROUND(AVG(c.overall_compliance_score), 1) AS product_compliance_score,
  SUM(c.subscription_count) AS total_subscriptions,
  COUNT(CASE WHEN c.subscription_count > 0 THEN 1 END) AS subscribed_assets,
  COUNT(CASE WHEN JSON_ARRAY_LENGTH(c.missing_required_forms) > 0 THEN 1 END) AS assets_with_form_gaps
FROM governance_metadata.asset_compliance c
JOIN latest ON c.snapshot_date = latest.d
GROUP BY COALESCE(NULLIF(c.data_product_name, ''), '(no data product)'), c.project_name
ORDER BY product_compliance_score ASC;
