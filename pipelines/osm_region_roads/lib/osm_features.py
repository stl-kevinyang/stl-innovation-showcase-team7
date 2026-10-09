"""Clip an OSM PBF to a polygon and extract highway way features via osmium-tool."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon, shape

from config import ROAD_FEATURE_TAGS

logger = logging.getLogger(__name__)


def require_osmium() -> str:
    path = shutil.which("osmium")
    if not path:
        raise RuntimeError(
            "osmium-tool is required. "
            "Install it (e.g. `sudo apt-get install osmium-tool`) and retry."
        )
    return path


def clip_pbf_to_polygon(
    source_pbf: Path,
    polygon_geojson: Path,
    clipped_pbf: Path,
    *,
    strategy: str = "complete_ways",
) -> Path:
    """Run `osmium extract -p` to clip a continent PBF to the region polygon."""
    osmium_bin = require_osmium()
    clipped_pbf.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        osmium_bin,
        "extract",
        "-p",
        str(polygon_geojson),
        "-s",
        strategy,
        "-o",
        str(clipped_pbf),
        "--overwrite",
        str(source_pbf),
    ]
    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return clipped_pbf


def filter_highway_ways(clipped_pbf: Path, highway_pbf: Path) -> Path:
    """Keep only ways (and referenced nodes) that have a highway tag."""
    osmium_bin = require_osmium()
    cmd = [
        osmium_bin,
        "tags-filter",
        "-o",
        str(highway_pbf),
        "--overwrite",
        str(clipped_pbf),
        "w/highway",
    ]
    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return highway_pbf


def export_geojsonseq(highway_pbf: Path, geojsonseq: Path) -> Path:
    """Export way linestrings as GeoJSONSeq (one Feature per line)."""
    osmium_bin = require_osmium()
    cmd = [
        osmium_bin,
        "export",
        "-f",
        "geojsonseq",
        "-o",
        str(geojsonseq),
        "--overwrite",
        "--geometry-types=linestring",
        "-a",
        "type,id",
        "-x",
        "print_record_separator=false",
        str(highway_pbf),
    ]
    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return geojsonseq


def _feature_to_row(
    feature: dict[str, Any],
    tags: Sequence[str],
    clip_geom: Polygon | MultiPolygon | None,
) -> dict[str, Any] | None:
    if feature.get("type") != "Feature":
        return None
    geom_obj = feature.get("geometry")
    if not geom_obj:
        return None
    geom = shape(geom_obj)
    if geom.is_empty or geom.geom_type not in {"LineString", "MultiLineString"}:
        return None
    if clip_geom is not None and not geom.intersects(clip_geom):
        return None

    props = feature.get("properties") or {}
    # With `-a type,id`, osmium puts numeric id under properties["id"].
    raw_id = props.get("id") or props.get("@id") or feature.get("id")
    osm_id: int | None
    if isinstance(raw_id, int):
        osm_id = raw_id
    elif isinstance(raw_id, str):
        osm_id = int(raw_id.rsplit("/", 1)[-1])
    else:
        osm_id = None

    row: dict[str, Any] = {"osm_id": osm_id, "geometry": geom}
    for tag in tags:
        row[tag] = props.get(tag)
    return row


def extract_road_features(
    clipped_pbf: Path,
    *,
    tags: Sequence[str] = ROAD_FEATURE_TAGS,
    clip_geom: Polygon | MultiPolygon | None = None,
    work_dir: Path | None = None,
) -> gpd.GeoDataFrame:
    """Parse highway ways from a (clipped) PBF into a GeoDataFrame in EPSG:4326."""
    base = work_dir or clipped_pbf.parent
    highway_pbf = base / f"{clipped_pbf.stem}_highway.osm.pbf"
    geojsonseq = base / f"{clipped_pbf.stem}_highway.geojsonseq"

    filter_highway_ways(clipped_pbf, highway_pbf)
    export_geojsonseq(highway_pbf, geojsonseq)

    rows: list[dict[str, Any]] = []
    with geojsonseq.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            # GeoJSONSeq may prefix records with RS (0x1e).
            if line[0] == "\x1e":
                line = line[1:]
            feature = json.loads(line)
            row = _feature_to_row(feature, tags, clip_geom)
            if row is not None:
                rows.append(row)

    if not rows:
        return gpd.GeoDataFrame(
            columns=["osm_id", *tags, "geometry"],
            geometry="geometry",
            crs="EPSG:4326",
        )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def write_features(gdf: gpd.GeoDataFrame, output: Path) -> Path:
    """Write features to GeoPackage, GeoJSON, or Parquet based on suffix."""
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    if suffix == ".gpkg":
        gdf.to_file(output, driver="GPKG")
    elif suffix in {".geojson", ".json"}:
        gdf.to_file(output, driver="GeoJSON")
    elif suffix == ".parquet":
        gdf.to_parquet(output)
    else:
        raise ValueError(
            f"Unsupported output format '{suffix}'. Use .gpkg, .geojson, or .parquet."
        )
    logger.info("Wrote %s road segments -> %s", len(gdf), output)
    return output
