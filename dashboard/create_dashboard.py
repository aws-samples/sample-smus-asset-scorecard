#!/usr/bin/env python3
"""
Create/refresh the QuickSight analysis + dashboard for the governance pipeline.

Resources:
  * Dataset  `gov-obs-compliance`        — latest snapshot only (current state).
  * Dataset  `gov-obs-compliance-trend`  — all snapshots (time series); created
                                            here if missing.
  * Analysis `gov-obs-analysis`
  * Dashboard `gov-obs-dashboard`

Idempotent: existing analysis/dashboard/trend-dataset are updated in place.
"""

import argparse
import sys
import time

import boto3
from botocore.exceptions import ClientError

# ── CLI arguments ─────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Create/refresh the governance QuickSight dashboard")
parser.add_argument("--profile",         default="smus",      help="AWS CLI profile (default: smus)")
parser.add_argument("--account-id",      required=True,       help="AWS account ID")
parser.add_argument("--region",          default="us-east-1", help="AWS region (default: us-east-1)")
parser.add_argument("--quicksight-user", required=True,       help="QuickSight username, e.g. Admin/john-Isengard")
parser.add_argument("--table-bucket-arn", required=True,      help="S3 Tables bucket ARN (arn:aws:s3tables:<region>:<account>:bucket/<name>)")
parser.add_argument("--workgroup",       default="gov-obs-wg", help="Athena workgroup for QuickSight queries (default: gov-obs-wg)")
args = parser.parse_args()

PROFILE    = args.profile
REGION     = args.region
ACCOUNT_ID = args.account_id

# QuickSight reaches the S3 Tables federated catalog THROUGH ATHENA. The native
# S3_TABLES connector only supports console-created ("new data preparation
# experience") datasets — API-created datasets fail at query time with a
# misleading IAM_ASSUME_ROLE_FAILURE. The Athena connector is fully scriptable
# and is the documented integration path for S3 Tables.
TABLE_BUCKET_NAME = args.table_bucket_arn.split("/")[-1]
TABLE_REF = f'"s3tablescatalog/{TABLE_BUCKET_NAME}"."governance_metadata"."asset_compliance"'

DATASOURCE_ID = "gov-obs-athena"
DATASOURCE_ARN = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:datasource/{DATASOURCE_ID}"

# Latest-snapshot dataset (already exists; used for current-state visuals).
DATASET_ID = "gov-obs-compliance"
DATASET_ARN = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:dataset/{DATASET_ID}"

# Trend dataset (all snapshots; created by this script if missing).
TREND_DATASET_ID = "gov-obs-compliance-trend"
TREND_DATASET_ARN = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:dataset/{TREND_DATASET_ID}"

ANALYSIS_ID = "gov-obs-analysis"
ANALYSIS_NAME = "SageMaker Catalog Governance Analysis"
DASHBOARD_ID = "gov-obs-dashboard"
DASHBOARD_NAME = "SageMaker Catalog Governance Dashboard"

USER_ARN = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:user/default/{args.quicksight_user}"

DS = "compliance"   # identifier for the latest-snapshot dataset in the definition
TREND = "trend"     # identifier for the trend dataset in the definition

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
qs = session.client("quicksight")


# ── display-name mapping ─────────────────────────────────────────────────────
# Technical column -> user-friendly label. The dataset SQL aliases columns to
# these labels, and `col()` translates technical names used in the visual code
# to the same labels, so the two stay consistent.
LABELS = {
    "asset_name": "Asset Name",
    "project_name": "Project Name",
    "project_id": "Project ID",
    "missing_required_forms": "Missing Required Forms",
    "dq_score": "Data Quality Score",
    "dq_rules_count": "DQ Rules Count",
    "glossary_terms_count": "Glossary Terms",
    "overall_compliance_score": "Compliance Score",
    "snapshot_date": "Snapshot Date",
    "compliance_band": "Compliance Band",
    "has_business_description": "Has Business Description",
    "has_resource_description": "Has Resource Description",
    "subscription_count": "Subscription Count",
    "risk_level": "Risk Level",
    "data_product_name": "Data Product",
}


# ── small builders ───────────────────────────────────────────────────────────
def col(name: str, ds: str = DS) -> dict:
    # Translate technical names to display labels; pass through helper columns
    # (e.g. the *_flag measures) that have no label.
    return {"DataSetIdentifier": ds, "ColumnName": LABELS.get(name, name)}


def title(text: str) -> dict:
    return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}


def num_measure(field_id: str, name: str, agg: str, ds: str = DS) -> dict:
    return {
        "NumericalMeasureField": {
            "FieldId": field_id,
            "Column": col(name, ds),
            "AggregationFunction": {"SimpleNumericalAggregation": agg},
        }
    }


def dim(field_id: str, name: str, ds: str = DS) -> dict:
    return {"CategoricalDimensionField": {"FieldId": field_id, "Column": col(name, ds)}}


# ── calculated fields (latest-snapshot dataset) ──────────────────────────────
CALCULATED_FIELDS = [
    {
        "DataSetIdentifier": DS,
        "Name": "form_gap_flag",
        # 1 when the asset is missing at least one required form ("[]" == none).
        "Expression": "ifelse({Missing Required Forms} = '[]', 0, 1)",
    },
    {
        "DataSetIdentifier": DS,
        "Name": "no_glossary_flag",
        # 1 when the asset has no glossary terms attached.
        "Expression": "ifelse({Glossary Terms} = 0, 1, 0)",
    },
]


# ── current-state visuals ────────────────────────────────────────────────────
def kpi_avg_score() -> dict:
    return {
        "KPIVisual": {
            "VisualId": "kpiAvgScore",
            "Title": title("Avg Compliance Score"),
            "ChartConfiguration": {
                "FieldWells": {"Values": [num_measure("kpi_avg", "overall_compliance_score", "AVERAGE")]}
            },
        }
    }


def kpi_no_glossary() -> dict:
    return {
        "KPIVisual": {
            "VisualId": "kpiNoGlossary",
            "Title": title("Assets Without Glossary"),
            "ChartConfiguration": {
                "FieldWells": {"Values": [num_measure("kpi_no_gloss", "no_glossary_flag", "SUM")]}
            },
        }
    }


def kpi_no_biz_desc() -> dict:
    return {
        "KPIVisual": {
            "VisualId": "kpiNoBizDesc",
            "Title": title("Assets Without Business Description"),
            "ChartConfiguration": {
                "FieldWells": {"Values": [num_measure("kpi_no_biz", "no_business_description_flag", "SUM")]}
            },
        }
    }


def kpi_no_res_desc() -> dict:
    return {
        "KPIVisual": {
            "VisualId": "kpiNoResDesc",
            "Title": title("Assets Without Resource Description"),
            "ChartConfiguration": {
                "FieldWells": {"Values": [num_measure("kpi_no_res", "no_resource_description_flag", "SUM")]}
            },
        }
    }


def donut_band() -> dict:
    return {
        "PieChartVisual": {
            "VisualId": "donutBand",
            "Title": title("Assets by Compliance Band"),
            "ChartConfiguration": {
                "FieldWells": {
                    "PieChartAggregatedFieldWells": {
                        "Category": [dim("donut_band", "compliance_band")],
                        "Values": [
                            {
                                "CategoricalMeasureField": {
                                    "FieldId": "donut_count",
                                    "Column": col("asset_name"),
                                    "AggregationFunction": "COUNT",
                                }
                            }
                        ],
                    }
                },
                "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
            },
        }
    }


def bar_assets_by_score() -> dict:
    return {
        "BarChartVisual": {
            "VisualId": "barScores",
            "Title": title("Assets by Compliance Score"),
            "ChartConfiguration": {
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [dim("bar_asset", "asset_name")],
                        "Values": [num_measure("bar_score", "overall_compliance_score", "SUM")],
                    }
                },
                "Orientation": "HORIZONTAL",
                "SortConfiguration": {
                    "CategorySort": [{"FieldSort": {"FieldId": "bar_score", "Direction": "ASC"}}]
                },
            },
        }
    }


def project_bar(visual_id: str, title_text: str, value_field_id: str,
                value_col: str, agg: str) -> dict:
    """Generic vertical bar: <agg>(value_col) grouped by project_name."""
    return {
        "BarChartVisual": {
            "VisualId": visual_id,
            "Title": title(title_text),
            "ChartConfiguration": {
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [dim(f"{visual_id}_proj", "project_name")],
                        "Values": [num_measure(value_field_id, value_col, agg)],
                    }
                },
                "SortConfiguration": {
                    "CategorySort": [{"FieldSort": {"FieldId": value_field_id, "Direction": "DESC"}}]
                },
            },
        }
    }


def table_all() -> dict:
    fields = [
        ("tbl_project", "project_name"),
        ("tbl_asset", "asset_name"),
        ("tbl_band", "compliance_band"),
        ("tbl_score", "overall_compliance_score"),
        ("tbl_missing", "missing_required_forms"),
        ("tbl_dq_score", "dq_score"),
        ("tbl_dq_rules", "dq_rules_count"),
        ("tbl_glossary", "glossary_terms_count"),
        ("tbl_subs", "subscription_count"),
        ("tbl_risk", "risk_level"),
        ("tbl_snapshot", "snapshot_date"),
    ]
    return {
        "TableVisual": {
            "VisualId": "tableAll",
            "Title": title("Asset Compliance Detail"),
            "ChartConfiguration": {
                "FieldWells": {
                    "TableUnaggregatedFieldWells": {
                        "Values": [{"FieldId": fid, "Column": col(name)} for fid, name in fields]
                    }
                }
            },
        }
    }


# ── subscription / consumption visuals ────────────────────────────────────────
def kpi_total_subscriptions() -> dict:
    return {
        "KPIVisual": {
            "VisualId": "kpiTotalSubs",
            "Title": title("Total Active Subscriptions"),
            "ChartConfiguration": {
                "FieldWells": {"Values": [num_measure("kpi_subs", "subscription_count", "SUM")]}
            },
        }
    }


def bar_most_consumed() -> dict:
    """Horizontal bar: assets ranked by subscription_count."""
    return {
        "BarChartVisual": {
            "VisualId": "barMostConsumed",
            "Title": title("Most Consumed Assets"),
            "ChartConfiguration": {
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [dim("subs_asset", "asset_name")],
                        "Values": [num_measure("subs_count", "subscription_count", "SUM")],
                    }
                },
                "Orientation": "HORIZONTAL",
                "SortConfiguration": {
                    "CategorySort": [{"FieldSort": {"FieldId": "subs_count", "Direction": "DESC"}}]
                },
            },
        }
    }


def donut_risk() -> dict:
    return {
        "PieChartVisual": {
            "VisualId": "donutRisk",
            "Title": title("Assets by Risk Level"),
            "ChartConfiguration": {
                "FieldWells": {
                    "PieChartAggregatedFieldWells": {
                        "Category": [dim("risk_cat", "risk_level")],
                        "Values": [
                            {
                                "CategoricalMeasureField": {
                                    "FieldId": "risk_count",
                                    "Column": col("asset_name"),
                                    "AggregationFunction": "COUNT",
                                }
                            }
                        ],
                    }
                },
                "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
            },
        }
    }


# ── data product visuals ──────────────────────────────────────────────────────
def bar_data_product_score() -> dict:
    """Bar chart: avg compliance score per data product."""
    return {
        "BarChartVisual": {
            "VisualId": "barDataProductScore",
            "Title": title("Compliance Score by Data Product"),
            "ChartConfiguration": {
                "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [dim("dp_cat", "data_product_name")],
                        "Values": [num_measure("dp_score", "overall_compliance_score", "AVERAGE")],
                    }
                },
                "Orientation": "HORIZONTAL",
                "SortConfiguration": {
                    "CategorySort": [{"FieldSort": {"FieldId": "dp_score", "Direction": "ASC"}}]
                },
            },
        }
    }


def table_data_products() -> dict:
    """Table: per-data-product compliance rollup."""
    fields = [
        ("tdp_product", "data_product_name"),
        ("tdp_project", "project_name"),
        ("tdp_score", "overall_compliance_score"),
        ("tdp_subs", "subscription_count"),
        ("tdp_risk", "risk_level"),
    ]
    return {
        "TableVisual": {
            "VisualId": "tableDataProducts",
            "Title": title("Data Product Compliance Rollup"),
            "ChartConfiguration": {
                "FieldWells": {
                    "TableAggregatedFieldWells": {
                        "GroupBy": [dim(fid, col) for fid, col in fields[:2]],
                        "Values": [
                            num_measure("tdp_avg_score", "overall_compliance_score", "AVERAGE"),
                            num_measure("tdp_total_subs", "subscription_count", "SUM"),
                        ],
                    }
                }
            },
        }
    }


# ── trend visual (all-snapshots dataset) ─────────────────────────────────────
def line_trend() -> dict:
    return {
        "LineChartVisual": {
            "VisualId": "lineTrend",
            "Title": title("Compliance Trend Over Time"),
            "ChartConfiguration": {
                "FieldWells": {
                    "LineChartAggregatedFieldWells": {
                        "Category": [dim("trend_date", "snapshot_date", TREND)],
                        "Values": [num_measure("trend_avg", "overall_compliance_score", "AVERAGE", TREND)],
                    }
                },
                "SortConfiguration": {
                    "CategorySort": [{"FieldSort": {"FieldId": "trend_date", "Direction": "ASC"}}]
                },
            },
        }
    }


# ── layout (36-column grid) ──────────────────────────────────────────────────
def grid_layout() -> dict:
    def el(vid, c, span, r, rspan):
        return {"ElementId": vid, "ElementType": "VISUAL",
                "ColumnIndex": c, "ColumnSpan": span, "RowIndex": r, "RowSpan": rspan}

    return {
        "Configuration": {
            "GridLayout": {
                "Elements": [
                    el("kpiAvgScore", 0, 9, 0, 6),
                    el("kpiNoGlossary", 9, 9, 0, 6),
                    el("kpiNoBizDesc", 18, 9, 0, 6),
                    el("kpiNoResDesc", 27, 9, 0, 6),
                    el("donutBand", 0, 12, 6, 8),
                    el("barScores", 12, 24, 6, 8),
                    el("barProjectAvg", 0, 18, 14, 8),
                    el("barProjectDqScore", 18, 18, 14, 8),
                    el("barProjectDqRules", 0, 18, 22, 8),
                    el("barProjectFormGap", 18, 18, 22, 8),
                    el("kpiTotalSubs", 0, 12, 30, 6),
                    el("barMostConsumed", 12, 24, 30, 6),
                    el("donutRisk", 0, 12, 36, 8),
                    el("barDataProductScore", 12, 24, 36, 8),
                    el("tableDataProducts", 0, 36, 44, 8),
                    el("lineTrend", 0, 36, 52, 8),
                    el("tableAll", 0, 36, 60, 12),
                ]
            }
        }
    }


def build_definition() -> dict:
    return {
        "DataSetIdentifierDeclarations": [
            {"DataSetArn": DATASET_ARN, "Identifier": DS},
            {"DataSetArn": TREND_DATASET_ARN, "Identifier": TREND},
        ],
        "CalculatedFields": CALCULATED_FIELDS,
        "Sheets": [
            {
                "SheetId": "governanceSheet",
                "Name": "Governance",
                "Visuals": [
                    kpi_avg_score(),
                    kpi_no_glossary(),
                    kpi_no_biz_desc(),
                    kpi_no_res_desc(),
                    donut_band(),
                    bar_assets_by_score(),
                    project_bar("barProjectAvg", "Avg Score by Project",
                                "bpa_val", "overall_compliance_score", "AVERAGE"),
                    project_bar("barProjectDqScore", "Avg DQ Score by Project",
                                "bpds_val", "dq_score", "AVERAGE"),
                    project_bar("barProjectDqRules", "Avg DQ Rules by Project",
                                "bpdr_val", "dq_rules_count", "AVERAGE"),
                    project_bar("barProjectFormGap", "Assets With Form Gaps by Project",
                                "bpfg_val", "form_gap_flag", "SUM"),
                    kpi_total_subscriptions(),
                    bar_most_consumed(),
                    donut_risk(),
                    bar_data_product_score(),
                    table_data_products(),
                    line_trend(),
                    table_all(),
                ],
                "Layouts": [grid_layout()],
            }
        ],
    }


# ── permission blocks ────────────────────────────────────────────────────────
DATASET_ACTIONS = [
    "quicksight:DescribeDataSet",
    "quicksight:DescribeDataSetPermissions",
    "quicksight:PassDataSet",
    "quicksight:DescribeIngestion",
    "quicksight:ListIngestions",
    "quicksight:UpdateDataSet",
    "quicksight:DeleteDataSet",
    "quicksight:CreateIngestion",
    "quicksight:CancelIngestion",
    "quicksight:UpdateDataSetPermissions",
]

ANALYSIS_ACTIONS = [
    "quicksight:RestoreAnalysis",
    "quicksight:UpdateAnalysisPermissions",
    "quicksight:DeleteAnalysis",
    "quicksight:DescribeAnalysisPermissions",
    "quicksight:QueryAnalysis",
    "quicksight:DescribeAnalysis",
    "quicksight:UpdateAnalysis",
]

DASHBOARD_ACTIONS = [
    "quicksight:DescribeDashboard",
    "quicksight:ListDashboardVersions",
    "quicksight:UpdateDashboardPermissions",
    "quicksight:QueryDashboard",
    "quicksight:UpdateDashboard",
    "quicksight:DeleteDashboard",
    "quicksight:DescribeDashboardPermissions",
    "quicksight:UpdateDashboardPublishedVersion",
]


# ── datasource (Athena — reaches S3 Tables via the federated catalog) ────────
def ensure_datasource(table_bucket_arn: str) -> None:
    """Create or update the Athena datasource used to query the S3 Table."""
    params = {
        "AwsAccountId": ACCOUNT_ID,
        "DataSourceId": DATASOURCE_ID,
        "Name": "SageMaker Governance (Athena)",
        "Type": "ATHENA",
        "DataSourceParameters": {
            "AthenaParameters": {
                "WorkGroup": args.workgroup,
            }
        },
        "Permissions": [
            {
                "Principal": USER_ARN,
                "Actions": [
                    "quicksight:DescribeDataSource",
                    "quicksight:DescribeDataSourcePermissions",
                    "quicksight:PassDataSource",
                    "quicksight:UpdateDataSource",
                    "quicksight:DeleteDataSource",
                    "quicksight:UpdateDataSourcePermissions",
                ],
            }
        ],
    }
    try:
        qs.create_data_source(**params)
        print(f"create_data_source: created ({DATASOURCE_ID})")
    except qs.exceptions.ResourceExistsException:
        qs.update_data_source(
            AwsAccountId=ACCOUNT_ID,
            DataSourceId=DATASOURCE_ID,
            Name=params["Name"],
            DataSourceParameters=params["DataSourceParameters"],
        )
        print(f"update_data_source: updated ({DATASOURCE_ID})")


# ── trend dataset (all snapshots) ────────────────────────────────────────────
def upsert_compliance_dataset() -> None:
    """Latest-snapshot dataset. Managed here so the custom SQL/columns (including
    the description flags and compliance_band) stay in sync with the pipeline."""
    sql = (  # nosec B608 - SQL built from internal constants, not user input
        'SELECT asset_name AS "Asset Name", project_name AS "Project Name", '
        'missing_required_forms AS "Missing Required Forms", dq_score AS "Data Quality Score", '
        'dq_rules_count AS "DQ Rules Count", glossary_terms_count AS "Glossary Terms", '
        'overall_compliance_score AS "Compliance Score", snapshot_date AS "Snapshot Date", '
        'has_business_description AS "Has Business Description", '
        'has_resource_description AS "Has Resource Description", '
        'COALESCE(subscription_count, 0) AS "Subscription Count", '
        'COALESCE(data_product_name, \'\') AS "Data Product", '
        "CASE WHEN has_business_description THEN 0 ELSE 1 END AS no_business_description_flag, "
        "CASE WHEN has_resource_description THEN 0 ELSE 1 END AS no_resource_description_flag, "
        "CASE WHEN overall_compliance_score >= 70 THEN 'Compliant' "
        "WHEN overall_compliance_score >= 50 THEN 'At Risk' "
        'ELSE \'Critical\' END AS "Compliance Band", '
        "CASE WHEN COALESCE(subscription_count, 0) >= 2 AND overall_compliance_score < 50 THEN 'Critical Risk' "
        "WHEN COALESCE(subscription_count, 0) >= 1 AND overall_compliance_score < 50 THEN 'Medium Risk' "
        'ELSE \'OK\' END AS "Risk Level" '
        f"FROM {TABLE_REF} "
        f"WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM {TABLE_REF})"  # nosec B608 - internal constant, not user input
    )
    physical = {
        "compliance-table": {
            "CustomSql": {
                "DataSourceArn": DATASOURCE_ARN,
                "Name": "asset_compliance_latest",
                "SqlQuery": sql,
                "Columns": [
                    {"Name": "Asset Name", "Type": "STRING"},
                    {"Name": "Project Name", "Type": "STRING"},
                    {"Name": "Missing Required Forms", "Type": "STRING"},
                    {"Name": "Data Quality Score", "Type": "DECIMAL"},
                    {"Name": "DQ Rules Count", "Type": "INTEGER"},
                    {"Name": "Glossary Terms", "Type": "INTEGER"},
                    {"Name": "Compliance Score", "Type": "DECIMAL"},
                    {"Name": "Snapshot Date", "Type": "STRING"},
                    {"Name": "Has Business Description", "Type": "BOOLEAN"},
                    {"Name": "Has Resource Description", "Type": "BOOLEAN"},
                    {"Name": "no_business_description_flag", "Type": "INTEGER"},
                    {"Name": "no_resource_description_flag", "Type": "INTEGER"},
                    {"Name": "Compliance Band", "Type": "STRING"},
                    {"Name": "Subscription Count", "Type": "INTEGER"},
                    {"Name": "Risk Level", "Type": "STRING"},
                    {"Name": "Data Product", "Type": "STRING"},
                ],
            }
        }
    }
    params = {
        "AwsAccountId": ACCOUNT_ID,
        "DataSetId": DATASET_ID,
        "Name": "Asset Compliance Scorecard",
        "ImportMode": "DIRECT_QUERY",
        "PhysicalTableMap": physical,
        "Permissions": [{"Principal": USER_ARN, "Actions": DATASET_ACTIONS}],
    }
    try:
        qs.create_data_set(**params)
        print(f"create_data_set: requested ({DATASET_ID})")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            upd = {k: v for k, v in params.items() if k != "Permissions"}
            qs.update_data_set(**upd)
            print(f"update_data_set: requested ({DATASET_ID}, already existed)")
        else:
            raise


def upsert_trend_dataset() -> None:
    sql = (  # nosec B608 - SQL built from internal constants, not user input
        'SELECT snapshot_date AS "Snapshot Date", project_name AS "Project Name", '
        'overall_compliance_score AS "Compliance Score", dq_score AS "Data Quality Score", '
        'dq_rules_count AS "DQ Rules Count", glossary_terms_count AS "Glossary Terms" '
        f"FROM {TABLE_REF}"  # nosec B608 - internal constant, not user input
    )
    physical = {
        "trend-table": {
            "CustomSql": {
                "DataSourceArn": DATASOURCE_ARN,
                "Name": "asset_compliance_all_snapshots",
                "SqlQuery": sql,
                "Columns": [
                    {"Name": "Snapshot Date", "Type": "STRING"},
                    {"Name": "Project Name", "Type": "STRING"},
                    {"Name": "Compliance Score", "Type": "DECIMAL"},
                    {"Name": "Data Quality Score", "Type": "DECIMAL"},
                    {"Name": "DQ Rules Count", "Type": "INTEGER"},
                    {"Name": "Glossary Terms", "Type": "INTEGER"},
                ],
            }
        }
    }
    params = {
        "AwsAccountId": ACCOUNT_ID,
        "DataSetId": TREND_DATASET_ID,
        "Name": "Asset Compliance Trend",
        "ImportMode": "DIRECT_QUERY",
        "PhysicalTableMap": physical,
        "Permissions": [{"Principal": USER_ARN, "Actions": DATASET_ACTIONS}],
    }
    try:
        qs.create_data_set(**params)
        print(f"create_data_set: requested ({TREND_DATASET_ID})")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            upd = {k: v for k, v in params.items() if k != "Permissions"}
            qs.update_data_set(**upd)
            print(f"update_data_set: requested ({TREND_DATASET_ID}, already existed)")
        else:
            raise


def upsert_analysis(definition: dict) -> None:
    params = {
        "AwsAccountId": ACCOUNT_ID,
        "AnalysisId": ANALYSIS_ID,
        "Name": ANALYSIS_NAME,
        "Definition": definition,
        "Permissions": [{"Principal": USER_ARN, "Actions": ANALYSIS_ACTIONS}],
    }
    try:
        qs.create_analysis(**params)
        print(f"create_analysis: requested ({ANALYSIS_ID})")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            upd = {k: v for k, v in params.items() if k != "Permissions"}
            qs.update_analysis(**upd)
            print(f"update_analysis: requested ({ANALYSIS_ID}, already existed)")
        else:
            raise


def upsert_dashboard(definition: dict) -> None:
    params = {
        "AwsAccountId": ACCOUNT_ID,
        "DashboardId": DASHBOARD_ID,
        "Name": DASHBOARD_NAME,
        "Definition": definition,
        "Permissions": [{"Principal": USER_ARN, "Actions": DASHBOARD_ACTIONS}],
        "DashboardPublishOptions": {
            "AdHocFilteringOption": {"AvailabilityStatus": "ENABLED"}
        },
    }
    try:
        qs.create_dashboard(**params)
        print(f"create_dashboard: requested ({DASHBOARD_ID})")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            upd = {k: v for k, v in params.items() if k != "Permissions"}
            qs.update_dashboard(**upd)
            print(f"update_dashboard: requested ({DASHBOARD_ID}, already existed)")
        else:
            raise


def _wait(describe_fn, extract_status, label: str) -> str:
    for _ in range(20):
        status = extract_status(describe_fn())
        if status in ("CREATION_SUCCESSFUL", "UPDATE_SUCCESSFUL"):
            return status
        if status in ("CREATION_FAILED", "UPDATE_FAILED"):
            raise RuntimeError(f"{label} {status}")
        time.sleep(3)
    return "TIMED_OUT_WAITING"


def main() -> int:
    ensure_datasource(args.table_bucket_arn)
    upsert_compliance_dataset()
    upsert_trend_dataset()

    definition = build_definition()

    upsert_analysis(definition)
    a_status = _wait(
        lambda: qs.describe_analysis(AwsAccountId=ACCOUNT_ID, AnalysisId=ANALYSIS_ID),
        lambda d: d["Analysis"]["Status"],
        "analysis",
    )
    print(f"analysis status: {a_status}")

    upsert_dashboard(definition)
    d_status = _wait(
        lambda: qs.describe_dashboard(AwsAccountId=ACCOUNT_ID, DashboardId=DASHBOARD_ID),
        lambda d: d["Dashboard"]["Version"]["Status"],
        "dashboard",
    )
    print(f"dashboard status: {d_status}")

    print("\n=== URLs ===")
    print(f"Analysis : https://{REGION}.quicksight.aws.amazon.com/sn/analyses/{ANALYSIS_ID}")
    print(f"Dashboard: https://{REGION}.quicksight.aws.amazon.com/sn/dashboards/{DASHBOARD_ID}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ClientError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
