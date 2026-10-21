CREATE OR REPLACE VIEW aq_gold.v_station_day AS
SELECT
    f.location_id,
    f.local_date,
    d.station_name,
    d.locality,
    d.latitude,
    d.longitude,
    f.pm25_avg,
    f.pm25_max,
    f.hours,
    f.fires_100km,
    f.hours >= 18 AS complete_day,
    f.pm25_avg > 37.5 AS over_thai_std,
    f.pm25_avg > 15 AS over_who
FROM aq_gold.fct_station_day f
LEFT JOIN aq_gold.dim_station d ON d.location_id = f.location_id
