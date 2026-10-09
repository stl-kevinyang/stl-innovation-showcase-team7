# stl-innovation-showcase-team7

Innovation showcase workspace for team 7.

## Pipelines

| Pipeline | Description |
|----------|-------------|
| [`pipelines/osm_region_roads`](pipelines/osm_region_roads) | Clip OSM from `gs://osm-map-archive` to a region, load highway tags into dell4db `innovation_team7`, and attach `ma_tt3` / `ma_gt3` annual metrics via Zone Library. |
