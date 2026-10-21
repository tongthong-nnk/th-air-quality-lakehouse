CREATE EXTERNAL TABLE IF NOT EXISTS aq_silver.locations (
    id bigint,
    name string,
    locality string,
    lat double,
    lon double,
    provider string)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://${lake_bucket}/raw/openaq_locations/'
