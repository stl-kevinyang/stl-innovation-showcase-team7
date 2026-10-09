#!/usr/bin/env python3
"""Extract OSM road features for a region into dell4db + join ma_tt3/ma_gt3 metrics.

Example:
  python extract_road_features.py \\
      --region-file ./regions/charlottesville.geojson \\
      --region-name charlottesville_va \\
      --vintage 25-Jan-31 \\
      --zl-table reference.zone_usca_osm_all_202501 \\
      --year 2024
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (  # noqa: E402
    DEFAULT_DAY_PART,
    DEFAULT_DAY_TYPE,
    DEFAULT_METRICS_YEAR,
    DEFAULT_SOURCE_REGION,
    DEFAULT_ZL_TABLE,
    OSM_BUCKET,
    OSM_GCS_PROJECT,
    ROAD_FEATURE_TAGS,
    SOURCE_REGIONS,
)
from lib.bq_metrics import fetch_segment_metrics  # noqa: E402
from lib.db import (  # noqa: E402
    apply_ddl,
    connect,
    insert_region,
    refresh_enriched_table,
    resolve_zl_segments,
    write_osm_road_ways,
    write_segment_metrics,
)
from lib.gcs import download_source_pbf, resolve_vintage  # noqa: E402
from lib.osm_features import clip_pbf_to_polygon, extract_road_features  # noqa: E402
from lib.region import (  # noqa: E402
    infer_source_regions,
    load_region_polygon,
    write_extract_geojson,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("osm_region_roads")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Clip an OSM snapshot from gs://osm-map-archive to a region polygon, "
            "load highway features into dell4db innovation_team7, and attach "
            "ma_tt3 / ma_gt3 annual metrics via Zone Library segment_id."
        )
    )
    p.add_argument(
        "--region-file",
        required=True,
        type=Path,
        help="Shapefile or GeoJSON defining the region of interest.",
    )
    p.add_argument(
        "--region-name",
        required=True,
        help="Name stored in innovation_team7.region.region_name.",
    )
    p.add_argument(
        "--vintage",
        default=None,
        help="Archive vintage directory (e.g. 25-Jan-31). Default: newest in bucket.",
    )
    p.add_argument(
        "--source-region",
        choices=SOURCE_REGIONS,
        default=None,
        help=f"Continent extract. Default: infer (fallback {DEFAULT_SOURCE_REGION}).",
    )
    p.add_argument(
        "--local-pbf",
        type=Path,
        default=None,
        help="Skip GCS download and use this local .osm.pbf instead.",
    )
    p.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="Directory for intermediate files (default: system temp dir).",
    )
    p.add_argument(
        "--bucket",
        default=OSM_BUCKET,
        help=f"GCS bucket name (default: {OSM_BUCKET}).",
    )
    p.add_argument(
        "--project",
        default=OSM_GCS_PROJECT,
        help=f"GCP project for clients (default: {OSM_GCS_PROJECT}).",
    )
    p.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download source PBF even if a cached copy exists.",
    )
    p.add_argument(
        "--zl-table",
        default=DEFAULT_ZL_TABLE,
        help=f"dell4db ZL table (default: {DEFAULT_ZL_TABLE}).",
    )
    p.add_argument(
        "--year",
        type=int,
        default=DEFAULT_METRICS_YEAR,
        help=f"Annual metrics year (default: {DEFAULT_METRICS_YEAR}).",
    )
    p.add_argument(
        "--day-type",
        type=int,
        default=DEFAULT_DAY_TYPE,
        help=f"Metrics day_type slice (default: {DEFAULT_DAY_TYPE} = average day).",
    )
    p.add_argument(
        "--day-part",
        type=int,
        default=DEFAULT_DAY_PART,
        help=f"Metrics day_part slice (default: {DEFAULT_DAY_PART} = all day).",
    )
    p.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Only load region + OSM ways; skip BigQuery metrics.",
    )
    return p


def run(args: argparse.Namespace) -> int:
    if not args.region_file.exists():
        raise FileNotFoundError(f"Region file not found: {args.region_file}")

    geom = load_region_polygon(args.region_file)
    source_regions = (
        [args.source_region] if args.source_region else infer_source_regions(geom)
    )
    logger.info("Source region(s): %s", ", ".join(source_regions))

    work_ctx = (
        tempfile.TemporaryDirectory(prefix="osm_region_roads_")
        if args.work_dir is None
        else None
    )
    work_dir = Path(work_ctx.name) if work_ctx else args.work_dir
    assert work_dir is not None
    work_dir.mkdir(parents=True, exist_ok=True)

    vintage: str | None = None
    try:
        poly_geojson = write_extract_geojson(geom, work_dir / "region.geojson")
        frames = []

        if args.local_pbf:
            source_pbfs = [(None, args.local_pbf)]
            vintage = args.vintage
        else:
            vintage = resolve_vintage(args.vintage, bucket=args.bucket)
            logger.info("Archive vintage: %s", vintage)
            cache_dir = work_dir / "pbf_cache"
            source_pbfs = [
                (
                    region,
                    download_source_pbf(
                        vintage,
                        region,
                        cache_dir,
                        bucket=args.bucket,
                        project=args.project,
                        force=args.force_download,
                    ),
                )
                for region in source_regions
            ]

        for region_label, source_pbf in source_pbfs:
            label = region_label or source_pbf.stem.replace(".", "_")
            clipped = work_dir / f"clipped_{label}.osm.pbf"
            clip_pbf_to_polygon(source_pbf, poly_geojson, clipped)
            frames.append(
                extract_road_features(
                    clipped,
                    tags=ROAD_FEATURE_TAGS,
                    clip_geom=geom,
                    work_dir=work_dir,
                )
            )

        import geopandas as gpd
        import pandas as pd

        if len(frames) == 1:
            gdf = frames[0]
        else:
            gdf = gpd.GeoDataFrame(
                pd.concat(frames, ignore_index=True),
                geometry="geometry",
                crs="EPSG:4326",
            )
            gdf = gdf.drop_duplicates(subset=["osm_id"], keep="first")

        logger.info("Extracted %s highway ways", len(gdf))

        conn = connect()
        try:
            apply_ddl(conn)
            region_id = insert_region(
                conn,
                region_name=args.region_name,
                geom=geom,
                osm_vintage=vintage,
                zl_table=args.zl_table,
                metrics_year=date(args.year, 1, 1),
            )
            n_ways = write_osm_road_ways(conn, region_id, gdf, tags=ROAD_FEATURE_TAGS)

            zl = resolve_zl_segments(conn, gdf["osm_id"].tolist(), args.zl_table)
            n_metrics = 0
            n_enriched = 0
            if args.skip_metrics:
                logger.info("Skipping BigQuery metrics (--skip-metrics)")
            elif zl.empty:
                logger.warning("No ZL segments resolved; skipping metrics")
            else:
                metrics = fetch_segment_metrics(
                    zl["segment_id"].tolist(),
                    year=args.year,
                    day_type=args.day_type,
                    day_part=args.day_part,
                    project=args.project,
                )
                if metrics.empty:
                    logger.warning("BigQuery returned no metric rows")
                else:
                    merged = metrics.merge(
                        zl[["segment_id", "zone_id", "osm_id"]],
                        on="segment_id",
                        how="left",
                    )
                    n_metrics = write_segment_metrics(conn, region_id, merged)

            n_enriched = refresh_enriched_table(conn, region_id)
        finally:
            conn.close()

        logger.info(
            "Done. region_id=%s ways=%s metrics=%s enriched=%s schema=innovation_team7",
            region_id,
            n_ways,
            n_metrics,
            n_enriched,
        )
        print(region_id)
        return region_id
    finally:
        if work_ctx is not None:
            work_ctx.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        logger.exception("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
