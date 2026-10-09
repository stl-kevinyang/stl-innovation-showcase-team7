"""Pull ma_tt3 / ma_gt3 annual segment metrics from BigQuery via bq CLI."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_DAY_TYPE = -3
DEFAULT_DAY_PART = -10
MA_TT3 = "stl-production.ma_tt3.segment_annual_data_output"
MA_GT3 = "stl-production.ma_gt3.segment_annual_data_output"


def fetch_segment_metrics(
    segment_ids: Iterable[int],
    *,
    year: int,
    day_type: int = DEFAULT_DAY_TYPE,
    day_part: int = DEFAULT_DAY_PART,
    project: str | None = "stl-datascience",
) -> pd.DataFrame:
    """Return annual headline metrics for segment_ids, with truck volume from ma_gt3."""
    ids = sorted({int(i) for i in segment_ids})
    empty_cols = [
        "segment_id",
        "year",
        "match_dir",
        "day_type",
        "day_part",
        "trips_volume",
        "truck_trips_volume",
        "avg_speed",
        "speed_percentiles",
    ]
    if not ids:
        return pd.DataFrame(columns=empty_cols)

    year_start = date(year, 1, 1).isoformat()
    chunks: list[pd.DataFrame] = []
    chunk_size = 5000

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i : i + chunk_size]
        # Build an inline array literal — safe because ids are ints we control.
        array_lit = "[" + ", ".join(str(x) for x in chunk) + "]"
        sql = f"""
        WITH segs AS (
          SELECT segment_id FROM UNNEST({array_lit}) AS segment_id
        ),
        truck AS (
          SELECT
            segment_id,
            year,
            match_dir,
            day_type,
            day_part,
            SUM(trips_volume) AS trips_volume
          FROM `{MA_GT3}`
          WHERE year = DATE '{year_start}'
            AND day_type = {day_type}
            AND day_part = {day_part}
            AND segment_id IN (SELECT segment_id FROM segs)
          GROUP BY segment_id, year, match_dir, day_type, day_part
        )
        SELECT
          a.segment_id,
          CAST(a.year AS STRING) AS year,
          a.match_dir,
          a.day_type,
          a.day_part,
          a.trips_volume,
          g.trips_volume AS truck_trips_volume,
          a.avg_speed,
          a.speed_percentiles
        FROM `{MA_TT3}` a
        JOIN segs s USING (segment_id)
        LEFT JOIN truck g
          ON a.segment_id = g.segment_id
         AND a.year = g.year
         AND a.match_dir = g.match_dir
         AND a.day_type = g.day_type
         AND a.day_part = g.day_part
        WHERE a.year = DATE '{year_start}'
          AND a.day_type = {day_type}
          AND a.day_part = {day_part}
        """
        logger.info(
            "Querying BQ metrics for %s segments (year=%s day_type=%s day_part=%s)",
            len(chunk),
            year,
            day_type,
            day_part,
        )
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as fh:
            fh.write(sql)
            sql_path = Path(fh.name)

        out_path = sql_path.with_suffix(".json")
        try:
            cmd = [
                "bq",
                "query",
                "--use_legacy_sql=false",
                "--format=json",
                f"--project_id={project or 'stl-datascience'}",
                "--max_rows=10000000",
            ]
            with sql_path.open() as stdin, out_path.open("w") as stdout:
                subprocess.run(cmd, check=True, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE, text=True)
            raw = out_path.read_text().strip()
            if not raw:
                chunks.append(pd.DataFrame(columns=empty_cols))
            else:
                records = json.loads(raw)
                chunks.append(pd.DataFrame(records))
        finally:
            sql_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)

    df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame(columns=empty_cols)
    if not df.empty:
        df["year"] = pd.to_datetime(df["year"]).dt.date
        for col in ("segment_id", "match_dir", "day_type", "day_part", "trips_volume", "truck_trips_volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "avg_speed" in df.columns:
            df["avg_speed"] = pd.to_numeric(df["avg_speed"], errors="coerce")
        # bq JSON encodes arrays as lists already; ensure list[int]
        if "speed_percentiles" in df.columns:
            df["speed_percentiles"] = df["speed_percentiles"].apply(
                lambda v: None
                if v is None or (isinstance(v, float) and pd.isna(v))
                else [int(x) for x in v]
            )
    logger.info("Fetched %s metric rows from BigQuery", len(df))
    return df
