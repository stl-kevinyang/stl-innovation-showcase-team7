-- dell4db schema for innovation showcase team 7 OSM + SATC extracts
CREATE SCHEMA IF NOT EXISTS innovation_team7;

CREATE TABLE IF NOT EXISTS innovation_team7.region (
    region_id    serial PRIMARY KEY,
    region_name  text NOT NULL,
    geom         geometry(MultiPolygon, 4326) NOT NULL,
    osm_vintage  text,
    zl_table     text,
    metrics_year date,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS innovation_team7_region_geom_idx
    ON innovation_team7.region USING gist (geom);

CREATE TABLE IF NOT EXISTS innovation_team7.segment_metrics (
    region_id          integer NOT NULL REFERENCES innovation_team7.region (region_id) ON DELETE CASCADE,
    segment_id         bigint NOT NULL,
    zone_id            bigint,
    osm_id             bigint NOT NULL,
    year               date NOT NULL,
    match_dir          integer NOT NULL,
    day_type           integer NOT NULL,
    day_part           integer NOT NULL,
    trips_volume       integer,
    truck_trips_volume integer,
    avg_speed          double precision,
    speed_percentiles  integer[],
    PRIMARY KEY (region_id, segment_id, year, match_dir, day_type, day_part)
);

CREATE INDEX IF NOT EXISTS innovation_team7_segment_metrics_osm_id_idx
    ON innovation_team7.segment_metrics (osm_id);

CREATE INDEX IF NOT EXISTS innovation_team7_segment_metrics_segment_id_idx
    ON innovation_team7.segment_metrics (segment_id);

-- road_segment_enriched is a materialized table (not a view).
-- Tag columns are created/ensured by the pipeline (lib/db.ensure_enriched_table).
-- Legacy view name is dropped on refresh if present.
DROP VIEW IF EXISTS innovation_team7.road_segment_enriched;
