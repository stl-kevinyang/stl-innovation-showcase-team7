"""Configuration for the OSM region road-feature extract pipeline."""

from __future__ import annotations

# GCS cold-storage archive (StreetLight convention; see refinery pull_osm_data DAG).
# Objects live at: gs://{OSM_BUCKET}/{vintage}/{source_region}.osm.pbf
# Example: gs://osm-map-archive/25-oct-01/north-america-latest.osm.pbf
OSM_GCS_PROJECT = "stl-datascience"
OSM_BUCKET = "osm-map-archive"

# Continent extracts stored in the archive (Geofabrik naming).
SOURCE_REGIONS = (
    "north-america-latest",
    "central-america-latest",
)

DEFAULT_SOURCE_REGION = "north-america-latest"

# Vintage directory format used by the archive ingest: "%y-%b-%d" (e.g. 25-oct-01).
VINTAGE_STRFTIME = "%y-%b-%d"

# OSM tags to keep on highway ways (road segments).
ROAD_FEATURE_TAGS: tuple[str, ...] = (
    "highway",
    "maxspeed",
    "maxspeed:forward",
    "maxspeed:backward",
    "maxspeed:conditional",
    "lanes",
    "lanes:forward",
    "lanes:backward",
    "width",
    "oneway",
    "sidewalk",
    "sidewalk:left",
    "sidewalk:right",
    "sidewalk:both",
    "sidewalk:width",
    "sidewalk:surface",
    "sidewalk:smoothness",
    "foot",
    "foot:forward",
    "foot:backward",
    "footway",
    "footway:left",
    "footway:right",
    "surface",
    "smoothness",
    "lit",
    "incline",
    "traffic_calming",
    "parking:lane",
    "parking:left",
    "parking:right",
    "parking:both",
    "shoulder",
    "shoulder:left",
    "shoulder:right",
    "junction",
    "access",
    "motor_vehicle",
    "motorcar",
    "vehicle",
    "service",
    "abutters",
    "landuse",
    "bridge",
    "tunnel",
    "layer",
    "railway",
    "cycleway",
    "cycleway:left",
    "cycleway:right",
    "cycleway:both",
    "construction",
    "proposed",
    "crossing",
    "crossing:markings",
    "crossing:signals",
    "crossing:island",
    "button_operated",
    "supervised",
    "flashing_lights",
    "traffic_signals",
    "traffic_signals:direction",
    "stop",
    "kerb",
    "tactile_paving",
    "segregated",
    "wheelchair",
    "handrail",
    "hazard",
    "traffic_sign",
    "amenity",
    "barrier",
    "public_transport",
    "leisure",
)

# Rough bbox hints for choosing which continent PBF to pull.
# Values are (min_lon, min_lat, max_lon, max_lat).
SOURCE_REGION_BBOXES: dict[str, tuple[float, float, float, float]] = {
    # Covers CONUS, Canada, AK, HI at Geofabrik north-america extent.
    "north-america-latest": (-180.0, 7.0, -20.0, 85.0),
    # Used for PR / VI and broader Central America extracts in StreetLight lockers.
    "central-america-latest": (-93.0, 5.0, -58.0, 22.0),
}

# dell4db targets
PG_SCHEMA = "innovation_team7"
DEFAULT_ZL_TABLE = "reference.zone_usca_osm_all_202501"
DEFAULT_METRICS_YEAR = 2024
DEFAULT_DAY_TYPE = -3  # average day
DEFAULT_DAY_PART = -10  # all day
DELL4DB_HOST = "gcp-db-02.streetlightdata.net"
DELL4DB_PORT = "6543"
DELL4DB_DB = "dell4db"
DELL4DB_USER = "postgres"
