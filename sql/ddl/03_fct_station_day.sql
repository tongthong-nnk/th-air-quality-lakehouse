CREATE TABLE IF NOT EXISTS aq_gold.fct_station_day (
    location_id bigint,
    local_date date,
    pm25_avg double,
    pm25_max double,
    hours bigint,
    fires_100km bigint,
    silver_ingested_at timestamp)
LOCATION 's3://${lake_bucket}/gold/fct_station_day/'
TBLPROPERTIES ('table_type' = 'ICEBERG', 'vacuum_max_snapshot_age_seconds' = '604800')
