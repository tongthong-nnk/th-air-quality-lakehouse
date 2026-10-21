-- Checks that run after every gold build. A pipeline can succeed while its data is wrong or has
-- stopped arriving, so these look at the data itself. Returns one row: the number of failed
-- checks and what they found. Step Functions sends an alert when `failed` is not 0.
WITH day_counts AS (
    SELECT local_date, count(*) AS stations
    FROM aq_gold.fct_station_day
    GROUP BY local_date
),
newest AS (
    SELECT local_date, stations,
           (SELECT approx_percentile(stations, 0.5) FROM day_counts p
            WHERE p.local_date BETWEEN n.local_date - INTERVAL '30' DAY
                                   AND n.local_date - INTERVAL '1' DAY) AS usual_stations
    FROM day_counts n
    ORDER BY local_date DESC
    LIMIT 1
),
results AS (
    -- Iceberg does not enforce keys, so a faulty MERGE could add a second row.
    SELECT count(*) > 0 AS failed,
           format('duplicate (location_id, local_date) keys: %d', count(*)) AS detail
    FROM (
        SELECT location_id, local_date FROM aq_gold.fct_station_day
        GROUP BY location_id, local_date
        HAVING count(*) > 1
    )

    -- Silver already rejects bad readings, so these would come from a bug in the gold SQL,
    -- such as a join that repeats rows.
    UNION ALL
    SELECT count(*) > 0, format('rows with impossible values: %d', count(*))
    FROM aq_gold.fct_station_day
    WHERE pm25_avg < 0 OR pm25_max > 1000 OR pm25_avg > pm25_max
       OR hours NOT BETWEEN 1 AND 24 OR fires_100km < 0

    -- OpenAQ's archive has stopped for weeks at a time, most recently in July 2026.
    UNION ALL
    SELECT coalesce(max(local_date) < current_date - INTERVAL '6' DAY, true),
           format('newest day: %s', coalesce(CAST(max(local_date) AS varchar), 'none'))
    FROM aq_gold.fct_station_day

    UNION ALL
    SELECT stations < 0.5 * usual_stations,
           format('stations on %s: %d, 30-day median %s', local_date, stations, usual_stations)
    FROM newest
)
SELECT
    count_if(failed) AS failed,
    coalesce(
        array_join(array_agg(detail ORDER BY detail) FILTER (WHERE failed), '; '),
        'none') AS details
FROM results
