CREATE TABLE IF NOT EXISTS aq_gold.dim_station (
    location_id bigint,
    station_name string,
    locality string,
    latitude double,
    longitude double,
    provider string,
    updated_at timestamp)
LOCATION 's3://${lake_bucket}/gold/dim_station/'
TBLPROPERTIES ('table_type' = 'ICEBERG', 'vacuum_max_snapshot_age_seconds' = '604800')
