# OSM region road features → dell4db

Clip an OSM snapshot from `gs://osm-map-archive` to a region polygon, load highway way tags into **dell4db** `innovation_team7`, and attach StreetLight annual segment metrics (`ma_tt3` + truck volume from `ma_gt3`) via Zone Library `segment_id` → `osm_id`.

## Archive

```text
gs://osm-map-archive/{vintage}/{source_region}.osm.pbf
```

Example: `gs://osm-map-archive/25-Jan-31/north-america-latest.osm.pbf` (Nearline).

## dell4db schema `innovation_team7`

| Table | Grain | Contents |
|-------|-------|----------|
| `region` | 1 row per run | study polygon, OSM vintage, ZL table, metrics year |
| `osm_road_ways` | OSM way | highway tags + geometry |
| `segment_metrics` | ZL segment | volume, truck volume, avg speed, speed percentiles |
| `road_segment_enriched` | ZL segment | **materialized** metrics joined to OSM tags + geometry (rebuilt per region at end of run) |

### `segment_id` → OSM

```text
ma_tt3.segment_annual_data_output.segment_id
  → reference.zone_usca_osm_all_YYYYMM (segment_id, zone_id, osm_id)
  → innovation_team7.osm_road_ways.osm_id
```

### Metrics slice

`segment_annual_data_output` has many temporal aggregates per segment. Default pull:

- `day_type = -3` (average day)
- `day_part = -10` (all day)
- `--year` (e.g. 2024)

| Feature | Source |
|---------|--------|
| Volume | `ma_tt3…trips_volume` |
| Truck volume | `ma_gt3…trips_volume` |
| Average speed | `ma_tt3…avg_speed` |
| Speed percentiles | `ma_tt3…speed_percentiles` |

## Prerequisites

- `osmium-tool` on `PATH`
- `gsutil` / gcloud auth that can read `gs://osm-map-archive`
- Network access to dell4db (`gcp-db-02.streetlightdata.net:6543`)
- BigQuery access to `stl-production.ma_tt3` / `ma_gt3`
- ~20 GB free disk for a North America PBF

```bash
cd pipelines/osm_region_roads
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python extract_road_features.py \
  --region-file ./regions/charlottesville.geojson \
  --region-name charlottesville_va \
  --vintage 25-Jan-31 \
  --zl-table reference.zone_usca_osm_all_202501 \
  --year 2024 \
  --work-dir ./work
```

Prints the new `region_id` on success.
