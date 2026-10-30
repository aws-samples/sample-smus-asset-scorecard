# Governance Observability Pipeline — CDK Deployment

This AWS CDK (Python) application deploys a daily governance-observability
pipeline for assets cataloged in Amazon SageMaker Unified Studio (SMUS).

The pipeline reads the SMUS **asset-metadata export**, enriches every asset with
governance signals pulled from the Amazon DataZone APIs (attached metadata
forms, glossary terms, business description, and data-quality summaries),
computes a weighted **compliance score (0–100)** per asset, and writes the
results to an Apache Iceberg table via Amazon Athena for reporting and dashboards.

## What the stack deploys

| Resource | Purpose |
| --- | --- |
| **AWS Lambda function** (`gov-obs-enrichment`) | Python 3.12 enrichment handler — reads the export, enriches each asset, scores it, and writes the Iceberg output table. Uses the AWS SDK for pandas (`awswrangler`) managed layer. 900 s timeout, 1024 MB. |
| **IAM execution role** | Least-privilege role for the Lambda: DataZone read, form-policy object read, Athena execution, scoped Glue Data Catalog access, Lake Formation credential vending, S3 Tables read, and Athena staging read/write. |
| **Athena workgroup** (`gov-obs-wg`) | Isolated workgroup with an enforced result location for the enrichment queries. |
| **EventBridge Scheduler schedule** | Invokes the Lambda once per day, after the SMUS metadata export lands. Timezone and hour are configurable. |

## Prerequisites

- **Python 3.12**
- **AWS CDK CLI** v2 (`npm install -g aws-cdk`)
- **AWS credentials** configured for the target account/region (for example via
  `aws configure`, `AWS_PROFILE`, or environment variables)
- An existing **SMUS / DataZone domain** with the asset-metadata export enabled
- An **S3 bucket** for the form-policy config object and for Athena staging
  (these may be the same bucket)

## Deploy

```bash
cd cdk/
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# One-time per account/region:
cdk bootstrap

cdk deploy \
  -c dz_domain_id=<your-domain-id> \
  -c policy_s3_uri=s3://<your-bucket>/form_policy.json \
  -c staging_s3_uri=s3://<your-bucket>/athena-staging/ \
  -c pandas_layer_arn=arn:aws:lambda:<region>:336392948345:layer:AWSSDKPandas-Python312:<version>
```

> **Region note:** `pandas_layer_arn` defaults to the **us-east-1** layer ARN. If
> you deploy to any other region you **must** override it with the AWS SDK for
> pandas layer ARN for that region. Find the current ARN for your region and
> Python version here:
> <https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html>

## Context parameters

All parameters are supplied through CDK context (`-c key=value`, or defaults in
`cdk.json`). `account` and `region` are resolved automatically from your CDK
environment and are not parameters.

| Parameter | Required | Default | Description |
| --- | --- | --- | --- |
| `dz_domain_id` | **Yes** | — | SMUS / DataZone domain ID whose assets are scored (e.g. `dzd-xxxxxxxxxxxx`). |
| `policy_s3_uri` | **Yes** | — | `s3://` URI of the form-policy config JSON object. |
| `staging_s3_uri` | **Yes** | — | `s3://` URI of the Athena query staging/results prefix. |
| `pandas_layer_arn` | No | us-east-1 AWSSDKPandas-Python312 ARN | AWS SDK for pandas managed layer ARN — **must match your region**. |
| `name_prefix` | No | `gov-obs` | Prefix applied to resource names. |
| `export_database` | No | `aws-sagemaker-catalog` | Logical export database name. |
| `export_table` | No | `asset` | Export table name. |
| `export_catalog` | No | `s3tablescatalog/aws-sagemaker-catalog` | Federated S3 Tables catalog (3-part name) for the export. |
| `export_schema` | No | `asset_metadata` | Export schema/namespace. |
| `export_s3tables_bucket` | No | `aws-sagemaker-catalog` | S3 Tables bucket backing the export (for IAM scoping). |
| `gov_database` | No | `governance_metadata` | Glue database for the compliance output table. |
| `gov_table` | No | `asset_compliance` | Compliance output table name. |
| `min_expected_dq_rules` | No | `3` | Minimum DQ rules per asset for a full rule-coverage score. |
| `scheduler_timezone` | No | `America/Sao_Paulo` | IANA timezone for the daily schedule. |
| `scheduler_cron_hour` | No | `5` | Hour of day (0–23) the schedule fires. |
| `lambda_code_path` | No | `../lambda` | Path to the directory containing `handler.py`, relative to the `cdk/` directory you run `cdk` from. Defaults to the repo-root `lambda/` folder (one level up). |

> **Shared Lambda source.** The handler lives once at the repo-root `lambda/handler.py`
> and is the single source of truth. Both this CDK stack (via `lambda_code_path`,
> default `../lambda`) and the Terraform deployment (`terraform/main.tf`, via
> `${path.module}/../lambda/handler.py`) reference that same file, so there is no
> duplicated handler to keep in sync.

## Post-deployment steps

Deploying the stack provisions the compute and permissions. A few one-time
data-plane setup steps are required before the first successful run. Replace the
placeholders (`<...>`) with your own values.

1. **Create the governance output table (Athena DDL).**
   Create the Iceberg table the Lambda writes to, in the `gov_database`
   (`governance_metadata` by default). For example:

   ```sql
   CREATE TABLE governance_metadata.asset_compliance (
       asset_id                 string,
       asset_name               string,
       resource_type_enum       string,
       project_id               string,
       project_name             string,
       missing_required_forms   string,
       dq_score                 double,
       dq_rules_count           int,
       dq_last_run              string,
       glossary_terms_count     int,
       overall_compliance_score double,
       snapshot_date            string
   )
   PARTITIONED BY (snapshot_date)
   LOCATION 's3://<your-bucket>/governance_metadata/asset_compliance/'
   TBLPROPERTIES ('table_type' = 'ICEBERG');
   ```

2. **Configure Lake Formation grants for the Lambda role.**
   Grant the execution role the Lake Formation permissions it needs to read the
   federated export catalog and to read/write the governance database
   (`DESCRIBE`/`SELECT` on the export, and `CREATE_TABLE`/`ALTER`/`DROP`/
   `SELECT`/`INSERT` on the governance database/tables).

3. **Add the Lambda role to the relevant DataZone project.**
   `GetAsset` visibility in SMUS comes from project membership. Add the Lambda
   execution role as a `PROJECT_CONTRIBUTOR` of the project that **owns** the
   assets you want to score:

   ```bash
   aws datazone create-project-membership \
     --domain-identifier <your-domain-id> \
     --project-identifier <owning-project-id> \
     --member '{"groupIdentifier":"<role-group-profile-id>"}' \
     --designation PROJECT_CONTRIBUTOR
   ```

   > In domains using IAM Identity Center, an IAM role is represented by an
   > auto-created user profile and a backing **group profile**. Add the group
   > profile (look it up with `aws datazone search-user-profiles
   > --user-type DATAZONE_IAM_USER --search-text <role-name>`), not the role ARN.

4. **Upload the form-policy config.**
   Upload your `form_policy.json` to the `policy_s3_uri` location. This file
   declares which metadata forms are mandatory, per project and resource type.

5. **Test with a manual invoke.**

   ```bash
   aws lambda invoke \
     --function-name gov-obs-enrichment \
     --cli-binary-format raw-in-base64-out \
     --payload '{"snapshot_date":"YYYY-MM-DD"}' \
     response.json && cat response.json
   ```

   A successful run returns the number of assets enriched. Spot-check the output:

   ```sql
   SELECT asset_id, asset_name, overall_compliance_score
   FROM governance_metadata.asset_compliance
   WHERE snapshot_date = 'YYYY-MM-DD'
   ORDER BY overall_compliance_score ASC;
   ```

## Clean up

```bash
cdk destroy
```

This removes the Lambda, IAM roles, Athena workgroup, and schedule. Resources
created by the post-deployment steps (the Glue/Iceberg table and its S3 data,
Lake Formation grants, project membership, and the uploaded policy object) are
**not** managed by the stack and must be removed separately if no longer needed.
