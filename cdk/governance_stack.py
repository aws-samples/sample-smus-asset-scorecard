"""
Governance Observability — CDK stack.

Provisions the infrastructure for the SageMaker Unified Studio (SMUS) governance
observability pipeline:

  * A Lambda function that reads the SMUS asset-metadata export, enriches every
    asset with form-coverage / data-quality / glossary signals via the DataZone
    APIs, computes a weighted compliance score, and writes the result to an
    Iceberg table through Athena.
  * The IAM execution role scoped to exactly those operations.
  * A dedicated Athena workgroup for the enrichment queries.
  * An EventBridge Scheduler schedule that runs the Lambda once a day, after the
    SMUS metadata export has landed.

All environment-specific values are supplied through CDK context (see cdk.json
or `-c key=value` on the CLI) so the same code can be deployed to any account,
region, or domain.
"""

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_athena as athena,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3tables as s3tables,
    aws_scheduler as scheduler,
)
from constructs import Construct
from cdk_nag import AwsSolutionsChecks, NagSuppressions


class GovernanceStack(Stack):
    """Main stack wiring together the enrichment Lambda, IAM, Athena, and schedule."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ──────────────────────────────────────────────────────────────────────
        # Configuration (CDK context with sensible defaults)
        #
        # `account` / `region` are taken from the deployment environment via the
        # CloudFormation pseudo-parameters (Aws.ACCOUNT_ID / Aws.REGION), so they
        # never need to be hard-coded.
        # ──────────────────────────────────────────────────────────────────────
        ctx = self.node.try_get_context

        # Required: the SMUS / DataZone domain whose assets are scored.
        dz_domain_id = ctx("dz_domain_id")
        if not dz_domain_id:
            raise ValueError(
                "Missing required context value 'dz_domain_id'. "
                "Pass it with: cdk deploy -c dz_domain_id=dzd-xxxxxxxxxxxx"
            )

        # Account / region are resolved from the CloudFormation pseudo-parameters
        # and used to build a globally-unique name for the governance bucket.
        account_id = Aws.ACCOUNT_ID
        region = Aws.REGION

        # Optional: naming and catalog/table identifiers (defaults match the POC).
        name_prefix = ctx("name_prefix") or "gov-obs"
        export_database = ctx("export_database") or "aws-sagemaker-catalog"
        export_table = ctx("export_table") or "asset"
        # The SMUS asset-metadata export is a federated S3 Tables catalog and must
        # be queried with a 3-part name: "<catalog>"."<schema>"."<table>".
        export_catalog = ctx("export_catalog") or "s3tablescatalog/aws-sagemaker-catalog"
        export_schema = ctx("export_schema") or "asset_metadata"
        gov_database = ctx("gov_database") or "governance_metadata"
        gov_table = ctx("gov_table") or "asset_compliance"

        # S3 Tables bucket for the enriched governance output table.
        # The Lambda writes the enriched Iceberg table here via awswrangler
        # using the s3tablescatalog federated catalog — matching the same format
        # used by the SMUS asset metadata export.
        #
        # The default embeds the account ID and region so the name is unique to
        # this account and cannot be squatted (an attacker cannot pre-create a
        # table bucket inside your account). This mirrors the naming of the
        # governance S3 bucket below. Override with -c gov_s3tables_bucket=<name>.
        gov_s3tables_bucket = ctx("gov_s3tables_bucket") or f"gov-obs-tables-{account_id}-{region}"

        # The S3 Tables bucket backing the SMUS metadata export. This is the
        # standard SMUS-managed catalog bucket name; override if yours differs.
        export_s3tables_bucket = ctx("export_s3tables_bucket") or "aws-sagemaker-catalog"

        # AWS SDK for pandas (awswrangler) managed layer. The default is the
        # us-east-1 / Python 3.12 / x86_64 ARN — you MUST override this with the
        # ARN for your deployment region. See:
        # https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
        # 336392948345 is the public AWS-owned account for the AWSSDKPandas managed
        # layer — it is NOT a personal/customer account ID.
        pandas_layer_arn = (
            ctx("pandas_layer_arn")
            or "arn:aws:lambda:us-east-1:336392948345:layer:AWSSDKPandas-Python312:29"
        )

        # Scoring / scheduling knobs.
        min_expected_dq_rules = str(ctx("min_expected_dq_rules") or 3)
        scheduler_timezone = ctx("scheduler_timezone") or "America/Sao_Paulo"
        scheduler_cron_hour = int(ctx("scheduler_cron_hour") or 5)

        # Path to the directory containing the Lambda source (handler.py). Defaults
        # to the shared `lambda/` folder at the repo root (one level up from cdk/),
        # which is the single source of truth for the handler.
        lambda_code_path = ctx("lambda_code_path") or "../lambda"

        # ──────────────────────────────────────────────────────────────────────
        # Governance S3 bucket
        #
        # Created and owned by this stack (no pre-existing bucket prerequisite).
        # Holds the form-policy config (uploaded below) and backs the Athena
        # staging/results prefix. Encrypted with SSE-S3, all public access
        # blocked, and removed with the stack.
        # ──────────────────────────────────────────────────────────────────────
        gov_bucket = s3.Bucket(
            self,
            "GovBucket",
            bucket_name=f"gov-obs-{account_id}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        NagSuppressions.add_resource_suppressions(
            gov_bucket,
            [
                {
                    "id": "AwsSolutions-S1",
                    "reason": "Server access logging not required for a sample code governance config bucket.",
                },
            ],
            apply_to_children=True,
        )

        # Derive the form-policy and Athena staging locations from the bucket the
        # stack just created, instead of taking them from context.
        # Users uploads form_policy.json here after deploy:
        # aws s3 cp config/form_policy.json s3://{bucket_name}/config/form_policy.json
        policy_s3_uri = f"s3://{gov_bucket.bucket_name}/config/form_policy.json"
        staging_s3_uri = f"s3://{gov_bucket.bucket_name}/athena-staging/"

        CfnOutput(
            self,
            "GovBucketName",
            value=gov_bucket.bucket_name,
            description="Governance S3 bucket",
        )

        CfnOutput(
            self,
            "UploadPolicyCommand",
            value=f"aws s3 cp config/form_policy.json s3://{gov_bucket.bucket_name}/config/form_policy.json",
            description="Run this after deploy to upload the form policy file",
        )

        # ──────────────────────────────────────────────────────────────────────
        # Athena workgroup
        #
        # Isolates the enrichment queries and pins their result location. The
        # configuration is enforced so the Lambda cannot accidentally write
        # results elsewhere.
        # ──────────────────────────────────────────────────────────────────────
        workgroup_name = f"{name_prefix}-wg"
        workgroup = athena.CfnWorkGroup(
            self,
            "GovWorkGroup",
            name=workgroup_name,
            recursive_delete_option=True,  # allow `cdk destroy` to remove it
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=staging_s3_uri,
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        # SSE-S3 requires no KMS key — S3 manages the encryption automatically.
                        # Use SSE_KMS here if your security policy requires customer-managed keys.
                        encryption_option="SSE_S3",
                    ),
                ),
            ),
        )

        # ──────────────────────────────────────────────────────────────────────
        # S3 Tables bucket for the enriched governance output
        #
        # The Lambda writes governance_metadata.asset_compliance here as an
        # Iceberg table via the s3tablescatalog federated catalog — same format
        # as the SMUS asset metadata export (aws-sagemaker-catalog).
        # ──────────────────────────────────────────────────────────────────────
        gov_table_bucket = s3tables.CfnTableBucket(
            self,
            "GovTableBucket",
            table_bucket_name=gov_s3tables_bucket,
        )

        # Referenced by the QuickSight dashboard script (README Part 6).
        CfnOutput(
            self,
            "S3TablesBucketArn",
            value=gov_table_bucket.attr_table_bucket_arn,
            description="S3 Tables bucket ARN for the enriched governance output",
        )

        # ──────────────────────────────────────────────────────────────────────
        # Namespace + table (created as part of the stack)
        #
        # The enrichment Lambda writes the governance output in S3 Tables mode via
        # plain Athena DML — `DELETE FROM` + `INSERT INTO` against
        # governance_metadata.asset_compliance. Athena DDL cannot CREATE TABLE
        # against the federated "s3tablescatalog/<bucket>" catalog, so the table
        # MUST exist (with a matching schema) before the first Lambda run.
        #
        # There are no L1 constructs for S3 Tables namespaces/tables yet, so we call
        # the S3 Tables API directly via AwsCustomResource. Parameter names follow
        # the AWS SDK for JavaScript v3 input shape (the custom-resource runtime),
        # e.g. `tableBucketARN` (ARN uppercased). The Iceberg schema below mirrors
        # the columns the handler inserts (see lambda/handler.py enrich_asset()).
        # ──────────────────────────────────────────────────────────────────────
        from aws_cdk import custom_resources as cr

        # Iceberg schema — column order/types match the DataFrame the handler writes.
        _iceberg_fields = [
            {"name": "asset_id", "type": "string", "required": False},
            {"name": "asset_name", "type": "string", "required": False},
            {"name": "resource_type_enum", "type": "string", "required": False},
            {"name": "project_id", "type": "string", "required": False},
            {"name": "project_name", "type": "string", "required": False},
            {"name": "missing_required_forms", "type": "string", "required": False},
            {"name": "dq_score", "type": "double", "required": False},
            {"name": "dq_rules_count", "type": "int", "required": False},
            {"name": "dq_last_run", "type": "string", "required": False},
            {"name": "glossary_terms_count", "type": "int", "required": False},
            {"name": "has_business_description", "type": "boolean", "required": False},
            {"name": "has_resource_description", "type": "boolean", "required": False},
            {"name": "subscription_count", "type": "int", "required": False},
            {"name": "subscriber_projects", "type": "string", "required": False},
            {"name": "data_product_name", "type": "string", "required": False},
            {"name": "overall_compliance_score", "type": "double", "required": False},
            {"name": "snapshot_date", "type": "string", "required": False},
        ]

        namespace_cr = cr.AwsCustomResource(
            self, "CreateNamespace",
            on_create=cr.AwsSdkCall(
                service="S3Tables",
                action="createNamespace",
                parameters={
                    "tableBucketARN": gov_table_bucket.attr_table_bucket_arn,
                    "namespace": [gov_database],
                },
                physical_resource_id=cr.PhysicalResourceId.of(gov_database),
            ),
            on_delete=cr.AwsSdkCall(
                service="S3Tables",
                action="deleteNamespace",
                parameters={
                    "tableBucketARN": gov_table_bucket.attr_table_bucket_arn,
                    "namespace": gov_database,
                },
                # Tolerate an already-deleted namespace so stack rollback/destroy
                # never wedges on a missing resource.
                ignore_error_codes_matching="NotFoundException",
            ),
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=[
                        "s3tables:CreateNamespace",
                        "s3tables:DeleteNamespace",
                    ],
                    resources=[
                        gov_table_bucket.attr_table_bucket_arn,
                        f"{gov_table_bucket.attr_table_bucket_arn}/*",
                    ],
                )
            ]),
        )
        namespace_cr.node.add_dependency(gov_table_bucket)

        table_cr = cr.AwsCustomResource(
            self, "CreateTable",
            on_create=cr.AwsSdkCall(
                service="S3Tables",
                action="createTable",
                parameters={
                    "tableBucketARN": gov_table_bucket.attr_table_bucket_arn,
                    "namespace": gov_database,
                    "name": gov_table,
                    "format": "ICEBERG",
                    "metadata": {"iceberg": {"schema": {"fields": _iceberg_fields}}},
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"{gov_database}.{gov_table}"
                ),
            ),
            on_delete=cr.AwsSdkCall(
                service="S3Tables",
                action="deleteTable",
                parameters={
                    "tableBucketARN": gov_table_bucket.attr_table_bucket_arn,
                    "namespace": gov_database,
                    "name": gov_table,
                },
                # Tolerate an already-deleted table so stack rollback/destroy
                # never wedges on a missing resource.
                ignore_error_codes_matching="NotFoundException",
            ),
            # Explicit policy instead of from_sdk_calls(): createTable with the
            # `metadata` (schema) parameter additionally requires
            # s3tables:PutTableData, which cannot be derived from the call name.
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=[
                        "s3tables:CreateTable",
                        "s3tables:PutTableData",
                        "s3tables:DeleteTable",
                    ],
                    resources=[
                        gov_table_bucket.attr_table_bucket_arn,
                        f"{gov_table_bucket.attr_table_bucket_arn}/*",
                    ],
                )
            ]),
        )
        table_cr.node.add_dependency(namespace_cr)

        # ──────────────────────────────────────────────────────────────────────
        # IAM execution role for the enrichment Lambda
        #
        # Granted only what the handler needs: read the DataZone catalog, read the
        # form-policy object, run Athena queries in the dedicated workgroup, manage
        # the Glue catalog tables it writes, vend Lake Formation credentials, read
        # the S3 Tables export, and read/write the Athena staging prefix.
        # ──────────────────────────────────────────────────────────────────────
        lambda_role = iam.Role(
            self,
            "EnrichmentRole",
            role_name=f"{name_prefix}-enrichment-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Execution role for the governance enrichment Lambda.",
        )

        # Referenced by the Lake Formation grants (README Part 4).
        CfnOutput(
            self,
            "EnrichmentRoleArn",
            value=lambda_role.role_arn,
            description="Execution role ARN of the enrichment Lambda (used in LF grants)",
        )
        # CloudWatch Logs permissions for the function.
        lambda_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        )

        # Read-only access to the DataZone domain. The handler reads the
        # PUBLISHED catalog surface (SearchListings/GetListing), which any
        # domain user can call with IAM alone — unlike GetAsset, which would
        # additionally require membership in every asset's owning project.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="DataZoneRead",
                actions=[
                    "datazone:SearchListings",
                    "datazone:GetListing",
                    "datazone:GetAssetType",
                    "datazone:ListProjects",
                    "datazone:ListSubscriptions",
                ],
                resources=[
                    f"arn:aws:datazone:{Aws.REGION}:{Aws.ACCOUNT_ID}:domain/{dz_domain_id}"
                ],
            )
        )

        # Read/write access to the governance bucket — covers the form-policy
        # config object and the Athena staging prefix. Granted via the bucket
        # construct so the policy tracks the bucket's actual ARN.
        gov_bucket.grant_read_write(lambda_role)

        # Run / inspect Athena queries within the dedicated workgroup.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="AthenaExecution",
                actions=[
                    "athena:StartQueryExecution",
                    "athena:GetQueryExecution",
                    "athena:GetQueryResults",
                    "athena:StopQueryExecution",
                    "athena:GetWorkGroup",
                ],
                resources=[
                    f"arn:aws:athena:{Aws.REGION}:{Aws.ACCOUNT_ID}:workgroup/{workgroup_name}"
                ],
            )
        )

        # Glue Data Catalog access. awswrangler's Iceberg write path creates and
        # replaces the governance output table, so CreateTable/DeleteTable are
        # required in addition to the read/update actions.
        #
        # Federated S3 Tables catalogs authorize against a dedicated ARN
        # hierarchy — catalog/s3tablescatalog/<bucket>,
        # database/s3tablescatalog/<bucket>/<namespace>, and
        # table/s3tablescatalog/<bucket>/<namespace>/<table>. Without these ARNs
        # a glue:GetCatalog denial surfaces in Athena as CATALOG_NOT_FOUND.
        #
        # Scoped to exactly what the handler touches:
        #   * the account catalog and the s3tablescatalog hierarchy for the
        #     export (read) and governance (write) table buckets;
        #   * the governance database that owns the compliance output table;
        #   * the tables within that governance database.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="GlueCatalog",
                actions=[
                    "glue:GetCatalog",
                    "glue:GetCatalogs",
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartitions",
                    "glue:UpdateTable",
                    "glue:BatchCreatePartition",
                    "glue:CreateTable",
                    "glue:DeleteTable",
                ],
                resources=[
                    # Account catalog — parent of every federated catalog lookup.
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:catalog",
                    # Federated S3 Tables catalog hierarchy (parent + per-bucket).
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:catalog/s3tablescatalog",
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:catalog/s3tablescatalog/{export_s3tables_bucket}",
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:catalog/s3tablescatalog/{gov_s3tables_bucket}",
                    # Export catalog: namespace + tables (read-only path).
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:database/s3tablescatalog/{export_s3tables_bucket}/{export_schema}",
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/s3tablescatalog/{export_s3tables_bucket}/{export_schema}/*",
                    # Governance catalog: namespace + tables (write path).
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:database/s3tablescatalog/{gov_s3tables_bucket}/{gov_database}",
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/s3tablescatalog/{gov_s3tables_bucket}/{gov_database}/*",
                    # Glue-mode fallback: the plain governance database/tables.
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:database/{gov_database}",
                    f"arn:aws:glue:{Aws.REGION}:{Aws.ACCOUNT_ID}:table/{gov_database}/*",
                ],
            )
        )

        # Lake Formation credential vending — required to read data registered
        # with Lake Formation (the SMUS export catalog and governed tables).
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="LakeFormationCredentialVending",
                actions=["lakeformation:GetDataAccess"],
                resources=["*"],
            )
        )

        # Read the S3 Tables catalog backing the SMUS asset-metadata export.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3TablesCatalogRead",
                actions=[
                    "s3tables:GetTableBucket",
                    "s3tables:GetNamespace",
                    "s3tables:GetTable",
                    "s3tables:GetTableData",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:ListNamespaces",
                    "s3tables:ListTables",
                ],
                resources=[
                    f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/{export_s3tables_bucket}",
                    f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/{export_s3tables_bucket}/*",
                ],
            )
        )
        # Write permissions to the governance output S3 Tables bucket.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3TablesGovWrite",
                actions=[
                    "s3tables:CreateNamespace",
                    "s3tables:CreateTable",
                    "s3tables:DeleteTableData",
                    "s3tables:GetTableBucket",
                    "s3tables:GetNamespace",
                    "s3tables:GetTable",
                    "s3tables:GetTableData",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:ListNamespaces",
                    "s3tables:ListTables",
                    "s3tables:PutTableData",
                    "s3tables:UpdateTableMetadataLocation",
                ],
                resources=[
                    f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/{gov_s3tables_bucket}",
                    f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/{gov_s3tables_bucket}/*",
                    f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/{export_s3tables_bucket}",
                    f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/{export_s3tables_bucket}/*",
                ],
            )
        )

        # ──────────────────────────────────────────────────────────────────────
        # Enrichment Lambda
        #
        # Single-file handler (handler.py) that depends on the AWS SDK for pandas
        # managed layer for awswrangler/pandas. 15-minute timeout and 1 GB memory
        # match the POC; for large catalogs, refactor the handler to fan out.
        # ──────────────────────────────────────────────────────────────────────
        enrichment_fn = lambda_.Function(
            self,
            "EnrichmentFunction",
            function_name=f"{name_prefix}-enrichment",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(lambda_code_path),
            role=lambda_role,
            timeout=Duration.seconds(900),
            memory_size=1024,
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(
                    self, "PandasLayer", pandas_layer_arn
                )
            ],
            environment={
                "DZ_DOMAIN_ID": dz_domain_id,
                "EXPORT_DATABASE": export_database,
                "EXPORT_TABLE": export_table,
                "EXPORT_CATALOG": export_catalog,
                "EXPORT_SCHEMA": export_schema,
                "GOV_DATABASE": gov_database,
                "GOV_TABLE": gov_table,
                "GOV_CATALOG": f"s3tablescatalog/{gov_s3tables_bucket}",
                "POLICY_S3_URI": policy_s3_uri,
                "ATHENA_WORKGROUP": workgroup_name,
                "ATHENA_STAGING_S3": staging_s3_uri,
                "MIN_EXPECTED_RULES": min_expected_dq_rules,
                # AWS_REGION is provided automatically by the Lambda runtime.
            },
        )
        # Ensure the workgroup exists before the function references it.
        enrichment_fn.node.add_dependency(workgroup)

        # ──────────────────────────────────────────────────────────────────────
        # EventBridge Scheduler — daily invocation
        #
        # Runs the enrichment once a day, shortly after the SMUS metadata export
        # lands. Timezone and hour are configurable so the schedule can align with
        # any region's export timing.
        # ──────────────────────────────────────────────────────────────────────
        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            role_name=f"{name_prefix}-scheduler-role",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Allows EventBridge Scheduler to invoke the enrichment Lambda.",
        )
        scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[enrichment_fn.function_arn],
            )
        )

        scheduler.CfnSchedule(
            self,
            "DailySchedule",
            name=f"{name_prefix}-daily-enrichment",
            description=(
                "Governance enrichment — runs daily after the SMUS export lands."
            ),
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF"
            ),
            schedule_expression=f"cron(0 {scheduler_cron_hour} * * ? *)",
            schedule_expression_timezone=scheduler_timezone,
            target=scheduler.CfnSchedule.TargetProperty(
                arn=enrichment_fn.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
        )

        # cdk-nag security checks for PCSR
        from aws_cdk import Aspects
        Aspects.of(self).add(AwsSolutionsChecks(verbose=True))

        # Suppress cdk-nag findings with documented justifications
        NagSuppressions.add_stack_suppressions(self, [
            {
                "id": "AwsSolutions-IAM4",
                "reason": "AWSLambdaBasicExecutionRole is acceptable for a sample code Lambda — no custom logging infrastructure required."
            },
            {
                "id": "AwsSolutions-L1",
                "reason": "Python 3.12 is intentionally pinned — the AWSSDKPandas managed Lambda layer is validated for 3.12. Moving to 3.13 requires layer re-validation."
            },
            {
                "id": "AwsSolutions-IAM5",
                "reason": "Wildcard permissions are required: (1) S3 Tables bucket ARNs require 'bucket/*' wildcard by design; (2) lakeformation:GetDataAccess does not support resource-level restrictions; (3) Glue wildcard covers all asset types managed by the governance pipeline."
            },
        ])
