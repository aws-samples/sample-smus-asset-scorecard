# Sample SMUS Asset Scorecard

> **Note:** This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory and compliance requirements before deployment.

Sample code for the AWS Big Data Blog post **"Go beyond the catalog: governance compliance in Amazon SageMaker Unified Studio"**.

This repository contains the enrichment pipeline that joins the SMUS asset metadata export with governance signals from the DataZone APIs to produce a per-asset compliance scorecard, stored as an Iceberg table in Amazon S3 Tables.

## Repository Structure

```
sample-smus-asset-scorecard/
├── lambda/
│   └── handler.py              # Enrichment Lambda — single source of truth
├── config/
│   └── form_policy.json        # Required forms per project (edit before deploying)
├── queries/
│   └── queries.sql             # Governance queries (compliance, risk, trend)
├── dashboard/
│   └── create_dashboard.py     # Amazon Quick Sight dashboard creation script
├── docs/
│   └── architecture.drawio     # Architecture diagram source
├── cdk/                        # CDK Python deployment
│   ├── app.py
│   ├── cdk.json
│   ├── governance_stack.py
│   ├── requirements.txt
│   └── README.md
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE
└── THIRD-PARTY-LICENSES
```

## Prerequisites

Before deploying, make sure you have:

- An **Amazon SageMaker Unified Studio domain** (IAM Identity Center-based) with at least one project and published assets.
- **AWS CLI ≥ 2.35.1** configured for your account and target region.
- **Python 3.12** and **AWS CDK v2** installed.
- **Amazon QuickSight** account in the same region (for Part 6), with admin
  access to **Manage QuickSight → Security & permissions** — the dashboard
  requires QuickSight's access to AWS services to be configured there
  (Amazon Athena enabled, the governance S3 bucket selected with Athena
  workgroup write permission, and the S3 Tables bucket selected). Part 6
  walks through each setting.
- **AWS Lake Formation** administrator access in your account.

## Walkthrough

### Part 1 — Enable the asset metadata export

Enable the export once per domain:

```bash
aws datazone put-data-export-configuration \
  --domain-identifier <domain-id> \
  --region <region> \
  --enable-export
```

> **Note:** The export is asynchronous and runs once daily. After enabling, wait up to 24 hours for the first export to complete before running the enrichment Lambda. This provisions the `aws-sagemaker-catalog` S3 table bucket managed by SMUS — you do not need to create it.

### Part 2 — Configure the form policy

Clone this repository and edit `config/form_policy.json` with your project IDs and required forms:

```json
{
  "default": {
    "required_forms": ["DataClassification"]
  },
  "projects": {
    "<project-id-1>": {
      "required_forms": ["DataClassification", "CriticalityForm"]
    },
    "<project-id-2>": {
      "required_forms": ["DataClassification", "CriticalityForm", "ExampleForm"]
    }
  }
}
```

You will upload this file after deploying the stack in the next step.

> **Note:** use form names that actually exist in your domain — if the policy
> requires forms no asset carries, every asset scores a form gap and the
> compliance bands flatten. You can list the form names your assets carry by
> querying the export's `extended_metadata` keys (they appear as
> `<FormName>.<field>`), for example:
>
> ```sql
> SELECT DISTINCT split_part(key, '.', 1) AS form_name
> FROM "s3tablescatalog/aws-sagemaker-catalog"."asset_metadata"."asset"
> CROSS JOIN UNNEST(map_keys(extended_metadata)) AS t(key)
> WHERE key != 'owningEntityId'
> ```

### Part 3 — Deploy the CDK stack

```bash
cd cdk
pip install -r requirements.txt

# Bootstrap CDK in your account/region (only needed once)
cdk bootstrap

# Deploy — creates the S3 bucket, S3 Tables bucket, namespace,
# table, Lambda, EventBridge Scheduler, IAM roles, and Athena workgroup
cdk deploy \
  -c dz_domain_id=<domain-id> \
  -c andas_layer_arn=arn:aws:lambda:<region>:336392948345:layer:AWSSDKPandas-Python312:<version>
```

> **Finding the `pandas_layer_arn`:** Use the [AWS SDK for pandas managed layers table](https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html) to find the ARN for Python 3.12, x86_64 in your region. For example, in `us-east-1`:
> ```
> arn:aws:lambda:us-east-1:336392948345:layer:AWSSDKPandas-Python312:29
> ```
> Note: `336392948345` is the public AWS-owned account that hosts the managed layer — it is not a personal account ID.

A successful deploy ends with output similar to this (the values are reused in
Parts 4 and 6):

```
✅  GovernanceStack

✨  Deployment time: 147.06s

Outputs:
GovernanceStack.EnrichmentRoleArn = arn:aws:iam::<account-id>:role/gov-obs-enrichment-role
GovernanceStack.GovBucketName = gov-obs-<account-id>-<region>
GovernanceStack.S3TablesBucketArn = arn:aws:s3tables:<region>:<account-id>:bucket/gov-obs-tables-<account-id>-<region>
GovernanceStack.UploadPolicyCommand = aws s3 cp config/form_policy.json s3://gov-obs-<account-id>-<region>/config/form_policy.json

✨  Total time: 233.11s
```

| Output | Used in |
|---|---|
| `EnrichmentRoleArn` | Part 4 — Lake Formation grants |
| `GovBucketName` | Part 6 — QuickSight staging bucket selection |
| `S3TablesBucketArn` | Part 6 — dashboard creation script |
| `UploadPolicyCommand` | Next step — form policy upload |

After the deploy completes, run the upload command from the outputs:

```bash
aws s3 cp config/form_policy.json s3://gov-obs-<account-id>-<region>/config/form_policy.json
```

### Part 4 — Grant Lake Formation permissions

The Lambda role needs Lake Formation grants on both S3 Tables catalogs before
the first run: the export table (read) and the enriched table (read/write).
IAM alone is not sufficient — Lake Formation enforces its own authorization on
top of IAM for these federated catalogs.

> **Note:** no DataZone project membership is required. The enrichment reads
> governance facets from the published catalog listings (`SearchListings` /
> `GetListing`), which are domain-wide readable — unlike the inventory API
> (`GetAsset`), which requires membership in every asset's owning project.

Grant the Lambda execution role read access to the metadata export:

```bash
LAMBDA_ROLE_ARN=<EnrichmentRoleArn-from-cdk-output>

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=$LAMBDA_ROLE_ARN \
  --resource '{"Database":{"CatalogId":"<account-id>:s3tablescatalog/aws-sagemaker-catalog","Name":"asset_metadata"}}' \
  --permissions "DESCRIBE" \
  --region <region>

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=$LAMBDA_ROLE_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/aws-sagemaker-catalog","DatabaseName":"asset_metadata","Name":"asset"}}' \
  --permissions "DESCRIBE" "SELECT" \
  --region <region>
```

Grant the Lambda execution role access to the enriched table:

```bash
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=$LAMBDA_ROLE_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" "INSERT" "DELETE" "ALTER" \
  --region <region>
```

Grant the QuickSight service role read access:

```bash
aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=arn:aws:iam::<account-id>:role/service-role/aws-quicksight-service-role-v0 \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" \
  --region <region>
```

### Part 5 — Run the first enrichment

Before running it, here is what the enrichment Lambda does. On each run it builds
one compliance record per published asset by joining the metadata export with
governance signals read from the DataZone catalog. The full implementation lives
in [`lambda/handler.py`](lambda/handler.py); this section walks through the key
steps so you know what to expect.

**1. Read the export snapshot (base inventory).** The Lambda queries the day's
export partition through Athena to get the set of assets to score, along with the
project each one belongs to and its descriptions:

```python
SELECT asset_id, asset_name, resource_type_enum,
       extended_metadata['owningEntityId'] AS project_id,
       business_description, resource_description
FROM "<export-catalog>"."asset_metadata"."asset"
WHERE DATE(snapshot_time) = DATE('<snapshot-date>')
```

**2. Resolve which forms are required (two levels).** "Required" is a two-level
concept. Level 1 is what SMUS itself enforces at the asset-type level (read with
`GetAssetType`); level 2 is the organization-specific requirements you declare in
`form_policy.json`. `required_forms()` takes the **union** of both — an asset must
satisfy every form from either layer:

```python
def required_forms(policy, project_id, resource_type) -> list[str]:
    platform_required = load_asset_type_required_forms(resource_type)  # GetAssetType
    org_required = ...  # from form_policy.json (type / project / default)
    return sorted(platform_required | org_required)
```

The form gap for an asset is simply the required forms that are not present on it.

**3. Read governance facets from the catalog listings.** The forms present on an
asset, its glossary terms, and its data quality summary are read from the
**published catalog**, not the project inventory. `build_listing_maps()` does one
`SearchListings` pass to map each asset to its listing (and to resolve
data-product membership at the same time); `fetch_listing_facets()` then reads
each listing with `GetListing`:

```python
def fetch_listing_facets(listing_id) -> dict:
    # GetListing → { present_forms, glossary_count, dq_summary }
```

> **Why listings and not `GetAsset`:** `GetAsset` is the inventory API and
> requires the Lambda role to be a member of each asset's owning project — scoring
> N projects would mean N project memberships. `SearchListings` / `GetListing`
> read the domain-wide published catalog with IAM permissions alone, so no
> per-project membership is needed. The trade-off: only *published* assets have
> listings, so unpublished inventory assets are scored on export-derived signals
> only.

**4. Extract the data quality signal (two dimensions).** Data quality is captured
as two independent dimensions: the **score** (how healthy the asset is) and the
**rule count** (how thoroughly it has been tested). They are kept separate on
purpose — an asset with a 95% score from a single rule is less well-governed than
one with 95% across 20 rules. Both ride on the listing's DQ summary, so no extra
API call is needed:

```python
def fetch_dq_details(dq_summary) -> tuple[float, int, str | None]:
    # returns (dq_score, dq_rules_count, dq_last_run)
```

**5. Compute the weighted compliance score (0–100).** `compute_score()` combines
the signals above into a single score across five dimensions:

| Dimension | Weight |
|---|---|
| Required forms present | 50% |
| Data quality score | 20% |
| Data quality rule coverage | 10% |
| Glossary term present | 10% |
| Business/resource description present | 10% |

The weights and the minimum DQ-rule threshold are Lambda environment variables, so
you can adjust them to reflect your organization's governance priorities.

**6. Write the result.** Each record is written to the
`governance_metadata.asset_compliance` table as a new `snapshot_date` partition.
The write is idempotent: the Lambda deletes the current day's partition before
inserting, so re-running for the same day replaces the data instead of
duplicating it.

Once the metadata export has completed, invoke the Lambda manually:

```bash
aws lambda invoke \
  --function-name gov-obs-enrichment \
  --region <region> \
  --cli-binary-format raw-in-base64-out \
  --log-type Tail \
  --payload '{}' \
  /tmp/response.json \
  --query 'LogResult' --output text | base64 -d | tail -20

cat /tmp/response.json
```

> **Note:** `--cli-binary-format raw-in-base64-out` is required on AWS CLI v2
> (the default expects a base64-encoded payload and fails on `'{}'` with an
> "Invalid base64" error).

A successful run returns:
```json
{"snapshot_date": "YYYY-MM-DD", "assets_enriched": N, "assets_with_form_gaps": N, "assets_with_dq": N}
```

### Part 6 — Build the QuickSight dashboard

QuickSight reaches the S3 Tables federated catalog **through Athena** — the
dashboard datasets run custom SQL against
`"s3tablescatalog/gov-obs-tables-<account-id>-<region>"."governance_metadata"."asset_compliance"`.
(The native QuickSight S3 Tables connector only supports datasets created
manually in the console, so this sample scripts the Athena path instead.)

Before running the script, give the QuickSight service role
(`aws-quicksight-service-role-v0`) the access it needs:

1. **Athena and the staging bucket** — in **Manage QuickSight → Security &
   permissions → QuickSight access to AWS services**, make sure **Amazon
   Athena** is enabled, and under **Amazon S3** select the
   `gov-obs-<account-id>-<region>` bucket.

   > ⚠️ **Selecting the bucket is not enough.** The bucket row has a second
   > checkbox, **"Write permission for Athena Workgroup"** — it MUST also be
   > checked. The dashboard queries run in the `gov-obs-wg` workgroup, which
   > writes results to the `athena-staging/` prefix of that bucket. If only
   > read access is granted, every dataset fails its connection test with:
   >
   > ```
   > Connection test failed: Could not get query execution ID:
   > Unable to verify/create output bucket gov-obs-<account-id>-<region>
   > (Service: Athena, Status Code: 400)
   > ```
2. **Glue catalog access** — resolving the federated S3 Tables catalog
   through Athena requires Glue and Lake Formation credential-vending
   permissions on the service role. Selecting your table buckets under
   **Amazon S3 Tables** in the same Security & permissions panel materializes
   these permissions: QuickSight creates and attaches a customer-managed
   policy named `AWSQuickSightGlueCatalogAccess` to the service role. Verify
   it is attached after saving:

   ```bash
   aws iam list-attached-role-policies \
     --role-name aws-quicksight-service-role-v0 \
     --query 'AttachedPolicies[].PolicyName'
   ```

   If it is missing, re-save the **Amazon S3 Tables** selection (deselect,
   save, re-select `gov-obs-tables-<account-id>-<region>`, save). See also the
   [S3 Tables + QuickSight documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-quicksight.html).
3. **Lake Formation grants for your QuickSight user** — QuickSight direct
   queries are authorized by Lake Formation against the **QuickSight user
   ARN** that owns the dataset, not only the service role. Without this
   grant the datasets fail with `CATALOG_NOT_FOUND: Catalog
   's3tablescatalog/gov-obs-tables-<account-id>-<region>' does not exist` (Lake Formation masks
   resources the principal cannot see):

   ```bash
   QS_USER_ARN=$(aws quicksight list-users --aws-account-id <account-id> \
     --namespace default --query 'UserList[0].Arn' --output text)

   aws lakeformation grant-permissions \
     --principal DataLakePrincipalIdentifier=$QS_USER_ARN \
     --resource '{"Database":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","Name":"governance_metadata"}}' \
     --permissions "DESCRIBE" \
     --region <region>

   aws lakeformation grant-permissions \
     --principal DataLakePrincipalIdentifier=$QS_USER_ARN \
     --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
     --permissions "DESCRIBE" "SELECT" \
     --region <region>
   ```

   > If you share the dashboard with other QuickSight users, each viewer's
   > direct queries are authorized the same way — grant additional viewers
   > Lake Formation access to the table as well.

Find your QuickSight username with:

```bash
aws quicksight list-users --aws-account-id <account-id> --namespace default \
  --query 'UserList[].UserName' --output text
```

Then run the dashboard creation script:

```bash
python3 dashboard/create_dashboard.py \
  --account-id <account-id> \
  --region <region> \
  --quicksight-user Admin/<iam-username> \
  --table-bucket-arn <S3TablesBucketArn-from-cdk-output>
```

### Part 7 — Validate in the SMUS query editor

The natural home for the governance queries is the SMUS query editor, where
your team already works. Queries there execute with the **project role**, so
grant that role read access to the enriched table. Find the project role ARN
in SMUS under **Project → Project details** (the IAM role follows the pattern
`datazone_usr_role_<project-id>_<environment-id>`):

```bash
PROJECT_ROLE_ARN=<project-role-arn>

aws lakeformation grant-permissions \
  --principal DataLakePrincipalIdentifier=$PROJECT_ROLE_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" \
  --region <region>
```

Then open the SMUS query editor in that project and run the queries from
`queries/queries.sql` against
`"s3tablescatalog/gov-obs-tables-<account-id>-<region>"."governance_metadata"."asset_compliance"`.

> **Note:** the same rule applies to any other principal that queries the
> table — Athena authorizes the signed-in identity (console and CLI behave
> the same), and Lake Formation authorizes whatever principal runs the query,
> even if it is the Lake Formation administrator that ran the Part 4 grants.
> To query from the plain Athena console or CLI, run the same grant with your
> own role or user ARN. (QuickSight is the exception: its queries are covered
> by the Part 6 grants.)

The EventBridge Scheduler runs automatically every day at 08:00 UTC — no further manual intervention needed.

## Cleaning up

Run the steps in this order — the Lake Formation revokes must happen **before**
`cdk destroy`, while the granted resources still exist.

**1. Remove the QuickSight resources:**

```bash
aws quicksight delete-dashboard --aws-account-id <account-id> --dashboard-id gov-obs-dashboard --region <region>
aws quicksight delete-analysis  --aws-account-id <account-id> --analysis-id  gov-obs-analysis  --region <region>
aws quicksight delete-data-set  --aws-account-id <account-id> --data-set-id  gov-obs-compliance --region <region>
aws quicksight delete-data-set  --aws-account-id <account-id> --data-set-id  gov-obs-compliance-trend --region <region>
aws quicksight delete-data-source --aws-account-id <account-id> --data-source-id gov-obs-athena --region <region>
```

**2. Revoke the Lake Formation grants** (Parts 4, 6, and 7 — the export-side
grants for the Lambda role, the enriched-table grants for the Lambda role,
the QuickSight service role, your QuickSight user, and your own principal):

```bash
LAMBDA_ROLE_ARN=<EnrichmentRoleArn>
QS_ROLE_ARN=arn:aws:iam::<account-id>:role/service-role/aws-quicksight-service-role-v0
QS_USER_ARN=<your-quicksight-user-arn>

# Part 4 — export side (Lambda role)
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=$LAMBDA_ROLE_ARN \
  --resource '{"Database":{"CatalogId":"<account-id>:s3tablescatalog/aws-sagemaker-catalog","Name":"asset_metadata"}}' \
  --permissions "DESCRIBE" --region <region>
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=$LAMBDA_ROLE_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/aws-sagemaker-catalog","DatabaseName":"asset_metadata","Name":"asset"}}' \
  --permissions "DESCRIBE" "SELECT" --region <region>

# Part 4 — enriched table (Lambda role, QuickSight service role)
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=$LAMBDA_ROLE_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" "INSERT" "DELETE" "ALTER" --region <region>
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=$QS_ROLE_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" --region <region>

# Part 6 — QuickSight user
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=$QS_USER_ARN \
  --resource '{"Database":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","Name":"governance_metadata"}}' \
  --permissions "DESCRIBE" --region <region>
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=$QS_USER_ARN \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" --region <region>

# Part 7 — your own querying principal
aws lakeformation revoke-permissions \
  --principal DataLakePrincipalIdentifier=<your-role-or-user-arn> \
  --resource '{"Table":{"CatalogId":"<account-id>:s3tablescatalog/gov-obs-tables-<account-id>-<region>","DatabaseName":"governance_metadata","Name":"asset_compliance"}}' \
  --permissions "DESCRIBE" "SELECT" --region <region>
```

**3. Remove the CDK-managed resources** (this also deletes the governance S3
bucket, including the uploaded `form_policy.json`, and the S3 Tables bucket
with the enriched table):

```bash
cd cdk
cdk destroy \
  -c dz_domain_id=<domain-id> \
  -c pandas_layer_arn=arn:aws:lambda:<region>:336392948345:layer:AWSSDKPandas-Python312:<version>
```

**[Optional] 4. Disable the metadata export** (skip if other tooling consumes it):

```bash
aws datazone put-data-export-configuration \
  --domain-identifier <domain-id> \
  --region <region> \
  --no-enable-export
```

> **Note:** after `cdk destroy`, the S3 Tables bucket name stays in a
> transitional state for a few minutes. If you redeploy immediately, the
> bucket creation fails with a 409 (`AlreadyExists`) and the stack rolls back.
> Delete the failed stack (`aws cloudformation delete-stack --stack-name
> GovernanceStack`), wait a few minutes, and deploy again.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security) for how to report security issues.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
