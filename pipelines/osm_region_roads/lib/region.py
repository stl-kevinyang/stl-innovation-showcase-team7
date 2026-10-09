"""Shapefile / polygon helpers for regional OSM extracts."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.ops import unary_union

from config import DEFAULT_SOURCE_REGION, SOURCE_REGION_BBOXES


def load_region_polygon(path: Path) -> Polygon | MultiPolygon:
    """Load a shapefile/GeoJSON and dissolve features into one (multi)polygon in WGS84."""
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise ValueError(f"Region file has no features: {path}")

    # Nominatim GeoJSON FeatureCollections often lack CRS metadata; assume WGS84.
    if gdf.crs is None:
        gdf = gdf.set_crs(epsg=4326)
    else:
        gdf = gdf.to_crs(epsg=4326)

    geom = unary_union(gdf.geometry.values)
    if geom.is_empty:
        raise ValueError(f"Dissolved region geometry is empty: {path}")

    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise ValueError(
            f"Region geometry must be Polygon/MultiPolygon after dissolve; got {geom.geom_type}"
        )
    return geom


def write_extract_geojson(geom: Polygon | MultiPolygon, path: Path) -> Path:
    """Write a GeoJSON FeatureCollection suitable for `osmium extract -p`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    feature = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": mapping(geom),
            }
        ],
    }
    import json

    path.write_text(json.dumps(feature))
    return path


def bbox_of(geom: Polygon | MultiPolygon) -> tuple[float, float, float, float]:
    """Return (min_lon, min_lat, max_lon, max_lat)."""
    minx, miny, maxx, maxy = geom.bounds
    return (minx, miny, maxx, maxy)


def _bbox_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def infer_source_regions(
    geom: Polygon | MultiPolygon,
    candidates: Iterable[str] | None = None,
) -> list[str]:
    """Pick archive continent PBF(s) whose rough extents overlap the region."""
    region_bbox = bbox_of(geom)
    names = list(candidates) if candidates is not None else list(SOURCE_REGION_BBOXES)
    hits = [
        name
        for name in names
        if name in SOURCE_REGION_BBOXES
        and _bbox_overlap(region_bbox, SOURCE_REGION_BBOXES[name])
    ]
    if not hits:
        return [DEFAULT_SOURCE_REGION]
    # Prefer north-america when both overlap (common for southern FL / Gulf).
    if "north-america-latest" in hits and "central-america-latest" in hits:
        # Puerto Rico / VI are intentionally on central-america in StreetLight lockers.
        minx, miny, maxx, maxy = region_bbox
        if miny < 20.0 and maxx > -70.0 and maxx < -60.0:
            return ["central-america-latest"]
    return hits
