#!/usr/bin/env python3
"""CDK application entry point for the governance observability pipeline.

Deploys the :class:`GovernanceStack` into the account/region resolved from your
standard AWS environment (CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION).

Required context (pass with ``-c key=value`` or set in ``cdk.json``):
    dz_domain_id         The SMUS / DataZone domain ID whose assets are scored.

Optional context:
    gov_s3tables_bucket  The S3 Tables bucket for the enriched governance output.
                         Defaults to gov-obs-tables-<account>-<region>, which is
                         unique to your account and cannot be squatted. Override
                         only if you need a custom name.

The governance S3 bucket (form-policy config + Athena staging) is created and
managed by the stack itself, so no S3 URIs need to be supplied.

Example:
    cdk deploy \\
        -c dz_domain_id=dzd-xxxxxxxxxxxx \\
        -c pandas_layer_arn=arn:aws:lambda:<region>:336392948345:layer:AWSSDKPandas-Python312:<n>

Note: 336392948345 is the public AWS-owned account that hosts the AWSSDKPandas
managed Lambda layer — it is NOT a personal/customer account ID.
See: https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
"""

import os

import aws_cdk as cdk

from governance_stack import GovernanceStack

app = cdk.App()

GovernanceStack(
    app,
    "GovernanceStack",
    # Resolve account/region from the ambient CDK environment. This lets the
    # stack deploy to any account/region without code changes.
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description="SMUS governance observability — enrichment Lambda, IAM, Athena, schedule.",
)

app.synth()
