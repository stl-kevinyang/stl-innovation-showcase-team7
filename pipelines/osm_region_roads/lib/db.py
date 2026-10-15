"""dell4db PostGIS helpers for innovation_team7."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable, Sequence

import geopandas as gpd
import pandas as pd
import psycopg2
from psycopg2 import sql
from shapely.geometry import MultiPolygon, Polygon

from config import ROAD_FEATURE_TAGS
from lib.bq_metrics import CLASS_COLUMNS

logger = logging.getLogger(__name__)

DEFAULT_PG = {
    "host": os.environ.get("DELL4DB_HOST", "gcp-db-02.streetlightdata.net"),
    "port": os.environ.get("DELL4DB_PORT", "6543"),
    "dbname": os.environ.get("DELL4DB_DB", "dell4db"),
    "user": os.environ.get("DELL4DB_USER", "postgres"),
    "password": os.environ.get("DELL4DB_PASSWORD", ""),
}

SCHEMA = "innovation_team7"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def connect(**overrides):
    params = {**DEFAULT_PG, **overrides}
    # Empty password -> omit so peer/trust auth works.
    if not params.get("password"):
        params.pop("password", None)
    return psycopg2.connect(**params)


def apply_ddl(conn, ddl_path: Path | None = None) -> None:
    if ddl_path is None:
        ddl_path = Path(__file__).resolve().parents[1] / "sql" / "01_create_schema.sql"
    ddl = ddl_path.read_text()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    ensure_class_columns(conn)


def ensure_class_columns(conn) -> None:
    """Add md_*/hd_* columns (and drop legacy truck_trips_volume) on existing tables."""
    add = ", ".join(f"ADD COLUMN IF NOT EXISTS {c} {pg}" for c, pg in CLASS_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {SCHEMA}.segment_metrics {add}")
        cur.execute(f"ALTER TABLE {SCHEMA}.segment_metrics DROP COLUMN IF EXISTS truck_trips_volume")
        cur.execute("SELECT to_regclass(%s)", (f"{SCHEMA}.road_segment_enriched",))
        if cur.fetchone()[0] is not None:
            cur.execute(f"ALTER TABLE {SCHEMA}.road_segment_enriched {add}")
            cur.execute(f"ALTER TABLE {SCHEMA}.road_segment_enriched DROP COLUMN IF EXISTS truck_trips_volume")
    conn.commit()


def _as_multipolygon(geom: Polygon | MultiPolygon) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    return MultiPolygon([geom])


def insert_region(
    conn,
    *,
    region_name: str,
    geom: Polygon | MultiPolygon,
    osm_vintage: str | None,
    zl_table: str | None,
    metrics_year,
) -> int:
    mp = _as_multipolygon(geom)
    wkt = mp.wkt
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {SCHEMA}.region
                (region_name, geom, osm_vintage, zl_table, metrics_year)
            VALUES
                (%s, ST_SetSRID(ST_GeomFromText(%s), 4326), %s, %s, %s)
            RETURNING region_id
            """,
            (region_name, wkt, osm_vintage, zl_table, metrics_year),
        )
        region_id = cur.fetchone()[0]
    conn.commit()
    logger.info("Inserted region_id=%s name=%s", region_id, region_name)
    return region_id


def _tag_column(tag: str) -> str:
    # Postgres-safe column names for OSM keys like maxspeed:forward
    col = tag.replace(":", "_").replace("-", "_")
    if not _IDENT_RE.match(col):
        raise ValueError(f"Unsafe tag column derived from {tag!r}: {col!r}")
    return col


def ensure_osm_road_ways_table(conn, tags: Sequence[str] = ROAD_FEATURE_TAGS) -> None:
    tag_cols = ",\n    ".join(f"{_tag_column(t)} text" for t in tags)
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.osm_road_ways (
        region_id integer NOT NULL REFERENCES {SCHEMA}.region (region_id) ON DELETE CASCADE,
        osm_id bigint NOT NULL,
        {tag_cols},
        geom geometry(LineString, 4326) NOT NULL,
        PRIMARY KEY (region_id, osm_id)
    );
    CREATE INDEX IF NOT EXISTS innovation_team7_osm_road_ways_geom_idx
        ON {SCHEMA}.osm_road_ways USING gist (geom);
    CREATE INDEX IF NOT EXISTS innovation_team7_osm_road_ways_osm_id_idx
        ON {SCHEMA}.osm_road_ways (osm_id);
    """
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def write_osm_road_ways(
    conn,
    region_id: int,
    gdf: gpd.GeoDataFrame,
    tags: Sequence[str] = ROAD_FEATURE_TAGS,
) -> int:
    ensure_osm_road_ways_table(conn, tags)
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {SCHEMA}.osm_road_ways WHERE region_id = %s",
            (region_id,),
        )

    if gdf.empty:
        conn.commit()
        return 0

    out = gdf.copy()
    out["region_id"] = region_id
    # Normalize geometry to LineString (take first if MultiLineString)
    def _line(g):
        if g is None or g.is_empty:
            return None
        if g.geom_type == "LineString":
            return g
        if g.geom_type == "MultiLineString":
            return max(g.geoms, key=lambda x: x.length)
        return None

    out["geometry"] = out.geometry.map(_line)
    out = out[out.geometry.notna()].copy()

    rename = {t: _tag_column(t) for t in tags}
    out = out.rename(columns=rename)
    out = out.rename_geometry("geom")
    cols = ["region_id", "osm_id", *rename.values(), "geom"]
    out = out[cols]

    tag_colnames = list(rename.values())
    placeholders = ", ".join(
        ["%s"] * (2 + len(tag_colnames)) + ["ST_SetSRID(ST_GeomFromText(%s), 4326)"]
    )
    col_list = ", ".join(["region_id", "osm_id", *tag_colnames, "geom"])
    insert_sql = f"INSERT INTO {SCHEMA}.osm_road_ways ({col_list}) VALUES ({placeholders})"

    rows = []
    for _, r in out.iterrows():
        rows.append(
            (
                int(r["region_id"]),
                int(r["osm_id"]),
                *[None if pd.isna(r[c]) else str(r[c]) for c in tag_colnames],
                r.geom.wkt,
            )
        )
    with conn.cursor() as cur:
        # Batch inserts for speed
        batch = 1000
        for i in range(0, len(rows), batch):
            cur.executemany(insert_sql, rows[i : i + batch])
            conn.commit()
    logger.info("Wrote %s osm_road_ways rows for region_id=%s", len(rows), region_id)
    return len(rows)


def resolve_zl_segments(
    conn,
    osm_ids: Iterable[int],
    zl_table: str,
) -> pd.DataFrame:
    """Return segment_id, zone_id, osm_id for osm_ids present in the ZL table."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", zl_table):
        raise ValueError(f"Invalid zl_table identifier: {zl_table}")

    ids = sorted({int(i) for i in osm_ids})
    if not ids:
        return pd.DataFrame(columns=["segment_id", "zone_id", "osm_id"])

    schema_name, table_name = zl_table.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                SELECT segment_id, zone_id, osm_id
                FROM {}.{}
                WHERE osm_id = ANY(%s)
                  AND COALESCE(is_current, true)
                """
            ).format(sql.Identifier(schema_name), sql.Identifier(table_name)),
            (ids,),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["segment_id", "zone_id", "osm_id"])
    logger.info("Resolved %s ZL segments from %s osm_ids via %s", len(df), len(ids), zl_table)
    return df


def _py(v):
    """Convert a pandas/numpy cell to a psycopg2-friendly value (NaN/None -> None)."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return list(v)
    if pd.isna(v):
        return None
    return v.item() if hasattr(v, "item") else v


def write_segment_metrics(conn, region_id: int, df: pd.DataFrame) -> int:
    ensure_class_columns(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {SCHEMA}.segment_metrics WHERE region_id = %s",
            (region_id,),
        )
    if df.empty:
        conn.commit()
        return 0

    class_names = [c for c, _ in CLASS_COLUMNS]
    cols = [
        "region_id",
        "segment_id",
        "zone_id",
        "osm_id",
        "year",
        "match_dir",
        "day_type",
        "day_part",
        "trips_volume",
        "avg_speed",
        "speed_percentiles",
        *class_names,
    ]
    out = df.copy()
    out["region_id"] = region_id
    out = out[cols]

    insert_sql = f"""
        INSERT INTO {SCHEMA}.segment_metrics (
            region_id, segment_id, zone_id, osm_id, year, match_dir, day_type, day_part,
            trips_volume, avg_speed, speed_percentiles, {", ".join(class_names)}
        ) VALUES (
            {", ".join(["%s"] * (11 + len(class_names)))}
        )
    """
    rows = []
    for _, r in out.iterrows():
        pct = r["speed_percentiles"]
        if pct is not None and not isinstance(pct, list):
            pct = list(pct)
        rows.append(
            (
                int(r["region_id"]),
                int(r["segment_id"]),
                None if pd.isna(r["zone_id"]) else int(r["zone_id"]),
                int(r["osm_id"]),
                r["year"],
                int(r["match_dir"]),
                int(r["day_type"]),
                int(r["day_part"]),
                None if pd.isna(r["trips_volume"]) else int(r["trips_volume"]),
                None if pd.isna(r["avg_speed"]) else float(r["avg_speed"]),
                pct,
                *[_py(r[c]) for c in class_names],
            )
        )
    with conn.cursor() as cur:
        cur.executemany(insert_sql, rows)
    conn.commit()
    logger.info("Wrote %s segment_metrics rows for region_id=%s", len(rows), region_id)
    return len(rows)


def ensure_enriched_table(conn) -> None:
    """Create materialized enriched table (drop legacy view if present)."""
    tag_cols = ",\n        ".join(f"{_tag_column(t)} text" for t in ROAD_FEATURE_TAGS)
    class_defs = ",\n                ".join(f"{c} {pg}" for c, pg in CLASS_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_views WHERE schemaname = '{SCHEMA}' AND viewname = 'road_segment_enriched') THEN
                    DROP VIEW {SCHEMA}.road_segment_enriched;
                END IF;
            END $$
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.road_segment_enriched (
                region_id          integer NOT NULL REFERENCES {SCHEMA}.region (region_id) ON DELETE CASCADE,
                segment_id         bigint NOT NULL,
                zone_id            bigint,
                osm_id             bigint NOT NULL,
                year               date NOT NULL,
                match_dir          integer NOT NULL,
                day_type           integer NOT NULL,
                day_part           integer NOT NULL,
                trips_volume       integer,
                avg_speed          double precision,
                speed_percentiles  integer[],
                {class_defs},
                {tag_cols},
                geom               geometry(LineString, 4326) NOT NULL,
                PRIMARY KEY (region_id, segment_id, year, match_dir, day_type, day_part)
            )
            """
        )
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS innovation_team7_enriched_osm_id_idx
                ON {SCHEMA}.road_segment_enriched (osm_id)
            """
        )
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS innovation_team7_enriched_geom_idx
                ON {SCHEMA}.road_segment_enriched USING gist (geom)
            """
        )
    conn.commit()
    ensure_class_columns(conn)


def refresh_enriched_table(conn, region_id: int) -> int:
    """Rebuild materialized road_segment_enriched rows for one region."""
    ensure_enriched_table(conn)
    class_names = ", ".join(c for c, _ in CLASS_COLUMNS)
    class_select = ", ".join(f"m.{c}" for c, _ in CLASS_COLUMNS)
    tag_cols = ", ".join(f"w.{_tag_column(t)}" for t in ROAD_FEATURE_TAGS)
    insert_sql = f"""
        INSERT INTO {SCHEMA}.road_segment_enriched (
            region_id, segment_id, zone_id, osm_id, year, match_dir, day_type, day_part,
            trips_volume, avg_speed, speed_percentiles,
            {class_names},
            {", ".join(_tag_column(t) for t in ROAD_FEATURE_TAGS)},
            geom
        )
        SELECT
            m.region_id,
            m.segment_id,
            m.zone_id,
            m.osm_id,
            m.year,
            m.match_dir,
            m.day_type,
            m.day_part,
            m.trips_volume,
            m.avg_speed,
            m.speed_percentiles,
            {class_select},
            {tag_cols},
            w.geom
        FROM {SCHEMA}.segment_metrics m
        JOIN {SCHEMA}.osm_road_ways w
          ON m.region_id = w.region_id AND m.osm_id = w.osm_id
        WHERE m.region_id = %s
    """
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {SCHEMA}.road_segment_enriched WHERE region_id = %s",
            (region_id,),
        )
        cur.execute(insert_sql, (region_id,))
        n = cur.rowcount
    conn.commit()
    logger.info("Materialized %s road_segment_enriched rows for region_id=%s", n, region_id)
    return n


# Backward-compatible alias used by older call sites.
def refresh_enriched_view(conn, region_id: int | None = None) -> int:
    if region_id is None:
        raise ValueError("region_id is required to refresh road_segment_enriched")
    return refresh_enriched_table(conn, region_id)
