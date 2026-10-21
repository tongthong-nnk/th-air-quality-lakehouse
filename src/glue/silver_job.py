"""Glue job: load one window of raw days into silver, behind a Glue Data Quality gate."""

import json
import sys

from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.transforms import SelectFromCollection
from awsglue.utils import getResolvedOptions
from awsgluedq.transforms import EvaluateDataQuality
from pyspark.conf import SparkConf
from pyspark.context import SparkContext

import silver

# Checked on every typed reading, before bad rows are split off. A few bad rows go to
# rejected_rows; this many failing means the source format changed, so the run stops.
RULES = """Rules = [
    RowCount > 0,
    Completeness "sensors_id" >= 0.99,
    Completeness "measured_at" >= 0.99,
    Completeness "value" >= 0.95
]"""

args = getResolvedOptions(sys.argv, ["lake_bucket", "lake_region", "start_date", "end_date"])
lake = f"s3://{args['lake_bucket']}"
CONF = {
    "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
    "spark.sql.catalog.glue_catalog": "org.apache.iceberg.spark.SparkCatalog",
    "spark.sql.catalog.glue_catalog.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
    "spark.sql.catalog.glue_catalog.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
    "spark.sql.catalog.glue_catalog.warehouse": f"{lake}/silver/",
    "spark.hadoop.fs.s3a.endpoint.region": args["lake_region"],
    "spark.sql.session.timeZone": "UTC",
}
glue = GlueContext(SparkContext(conf=SparkConf().setAll(CONF.items())))


def check_quality(readings):
    """Names of the rules that failed. Results also show on the job's Data quality tab."""
    result = EvaluateDataQuality().process_rows(
        frame=DynamicFrame.fromDF(readings, glue, "readings"),
        ruleset=RULES,
        publishing_options={
            "dataQualityEvaluationContext": "silver_readings",
            "enableDataQualityCloudWatchMetrics": False,
            "enableDataQualityResultsPublishing": True,
        },
    )
    outcomes = SelectFromCollection.apply(dfc=result, key="ruleOutcomes").toDF()
    return [row["Rule"] for row in outcomes.where("Outcome != 'Passed'").collect()]


counts = silver.run(
    glue.spark_session, lake, args["start_date"], args["end_date"], "glue_catalog", check_quality
)
print(json.dumps(counts))
