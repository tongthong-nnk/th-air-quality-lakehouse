import os

import pytest

# Same Iceberg release as AWS Glue 5.1, so the MERGE and DDL tests run against the real library.
ICEBERG_PACKAGE = "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.10.0"


@pytest.fixture(scope="session")
def spark(tmp_path_factory):
    """Local Spark with an Iceberg catalog named `local` in a temp folder."""
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.master("local[1]")
        .appName("tests")
        .config("spark.jars.packages", ICEBERG_PACKAGE)
        .config(
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        )
        .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.local.type", "hadoop")
        .config("spark.sql.catalog.local.warehouse", str(tmp_path_factory.mktemp("warehouse")))
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    session.sql("CREATE NAMESPACE IF NOT EXISTS local.aq_silver")
    yield session
    session.stop()


@pytest.fixture
def aws(monkeypatch):
    """Fake AWS (moto) with dummy credentials, so no test can reach a real account."""
    from moto import mock_aws

    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "ap-southeast-1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield os.environ["AWS_DEFAULT_REGION"]
