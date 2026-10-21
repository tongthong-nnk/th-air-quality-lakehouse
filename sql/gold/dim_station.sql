-- Plan rewrites the locations file every run with active stations only. Merging it into a table
-- instead of reading the file directly keeps the names of stations that have stopped reporting.
MERGE INTO aq_gold.dim_station t
USING (
    SELECT id AS location_id, name AS station_name, locality, lat AS latitude,
           lon AS longitude, provider
    FROM aq_silver.locations
) s
ON t.location_id = s.location_id
WHEN MATCHED AND (
    t.station_name IS DISTINCT FROM s.station_name
    OR t.locality IS DISTINCT FROM s.locality
    OR t.latitude IS DISTINCT FROM s.latitude
    OR t.longitude IS DISTINCT FROM s.longitude
    OR t.provider IS DISTINCT FROM s.provider
) THEN UPDATE SET
    station_name = s.station_name,
    locality = s.locality,
    latitude = s.latitude,
    longitude = s.longitude,
    provider = s.provider,
    updated_at = CAST(current_timestamp AS timestamp(6))
WHEN NOT MATCHED THEN INSERT
    (location_id, station_name, locality, latitude, longitude, provider, updated_at)
    VALUES (s.location_id, s.station_name, s.locality, s.latitude, s.longitude, s.provider,
            CAST(current_timestamp AS timestamp(6)))
