-- Rebuilds every day whose silver rows changed since the last build, so a daily run, a re-run
-- and a backfill all use this one statement with no dates passed in. The watermark is the newest
-- silver ingested_at already used, not the time of the last build: a silver load that commits
-- while this statement runs is then still picked up by the next build.
MERGE INTO aq_gold.fct_station_day t
USING (
    WITH watermark AS (
        SELECT coalesce(max(silver_ingested_at), TIMESTAMP '1970-01-01 00:00:00') AS seen
        FROM aq_gold.fct_station_day
    ),
    changed_days AS (
        SELECT local_date, max(ingested_at) AS ingested_at
        FROM (
            SELECT local_date, ingested_at FROM aq_silver.measurements
            UNION ALL
            SELECT local_date, ingested_at FROM aq_silver.fire_hotspots
        )
        WHERE ingested_at > (SELECT seen FROM watermark)
        GROUP BY local_date
    ),
    station_day AS (
        SELECT
            m.location_id,
            m.local_date,
            avg(m.lat) AS lat,
            avg(m.lon) AS lon,
            round(avg(m.value), 1) AS pm25_avg,
            round(max(m.value), 1) AS pm25_max,
            -- A station with two PM2.5 sensors reports each hour twice.
            count(DISTINCT m.measured_at) AS hours,
            max(c.ingested_at) AS ingested_at
        FROM aq_silver.measurements m
        JOIN changed_days c ON c.local_date = m.local_date
        WHERE m.parameter = 'pm25'
        GROUP BY m.location_id, m.local_date
    )
    SELECT
        s.location_id,
        s.local_date,
        s.pm25_avg,
        s.pm25_max,
        s.hours,
        count(f.local_date) AS fires_100km,
        CAST(s.ingested_at AS timestamp(6)) AS silver_ingested_at
    FROM station_day s
    LEFT JOIN aq_silver.fire_hotspots f
        ON f.local_date = s.local_date
        -- Low-confidence detections are mostly sun glint and hot surfaces, not fires.
        AND f.confidence IN ('n', 'h')
        AND great_circle_distance(s.lat, s.lon, f.latitude, f.longitude) <= 100
    GROUP BY s.location_id, s.local_date, s.pm25_avg, s.pm25_max, s.hours, s.ingested_at
) s
ON t.location_id = s.location_id AND t.local_date = s.local_date
WHEN MATCHED THEN UPDATE SET
    pm25_avg = s.pm25_avg,
    pm25_max = s.pm25_max,
    hours = s.hours,
    fires_100km = s.fires_100km,
    silver_ingested_at = s.silver_ingested_at
WHEN NOT MATCHED THEN INSERT
    (location_id, local_date, pm25_avg, pm25_max, hours, fires_100km, silver_ingested_at)
    VALUES (s.location_id, s.local_date, s.pm25_avg, s.pm25_max, s.hours, s.fires_100km,
            s.silver_ingested_at)
