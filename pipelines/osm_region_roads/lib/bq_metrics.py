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

# Per-weight-class metrics pulled from ma_gt3: (output suffix, ma_gt3 column, postgres type).
# Each is emitted twice, prefixed md_ (Medium Duty) and hd_ (Heavy Duty). Light Duty is excluded.
WEIGHT_CLASSES = {"md": "Medium Duty", "hd": "Heavy Duty"}
CLASS_METRICS = [
    ("truck_volume", "trips_volume", "integer"),
    ("avg_speed_mph", "avg_speed_mph", "double precision"),
    ("speed_percentiles", "speed_percentiles", "integer[]"),
    ("avg_travel_time_secs", "avg_travel_time_secs", "double precision"),
    ("travel_time_percentiles", "travel_time_percentiles", "double precision[]"),
    ("free_flow_speed", "free_flow_speed", "double precision"),
    ("free_flow_factor", "free_flow_factor", "double precision"),
    ("travel_time_free_flow", "travel_time_free_flow", "double precision"),
    ("truck_travel_time_reliability", "truck_travel_time_reliability", "double precision"),
    ("vmt", "vmt", "double precision"),
    ("vhd", "vhd", "double precision"),
]
# (output column name, postgres type), e.g. ("md_truck_volume", "integer")
CLASS_COLUMNS = [(f"{pre}_{out}", pg) for pre in WEIGHT_CLASSES for out, _, pg in CLASS_METRICS]
CLASS_ARRAY_COLUMNS = [c for c, pg in CLASS_COLUMNS if pg.endswith("[]")]
CLASS_INT_ARRAY_COLUMNS = [c for c in CLASS_ARRAY_COLUMNS if "travel_time" not in c]
CLASS_SCALAR_COLUMNS = [c for c, pg in CLASS_COLUMNS if not pg.endswith("[]")]


def _class_cte(prefix: str, weight_class: str, array_lit: str, year_start: str, day_type: int, day_part: int) -> str:
    cols = ",\n            ".join(f"{src} AS {prefix}_{out}" for out, src, _ in CLASS_METRICS)
    return f"""{prefix} AS (
          SELECT
            segment_id,
            year,
            match_dir,
            day_type,
            day_part,
            {cols}
          FROM `{MA_GT3}`
          WHERE year = DATE '{year_start}'
            AND day_type = {day_type}
            AND day_part = {day_part}
            AND weight_class = '{weight_class}'
            AND segment_id IN UNNEST({array_lit})
          QUALIFY ROW_NUMBER() OVER (
            PARTITION BY segment_id, year, match_dir, day_type, day_part
            ORDER BY trips_volume DESC
          ) = 1
        )"""


def fetch_segment_metrics(
    segment_ids: Iterable[int],
    *,
    year: int,
    day_type: int = DEFAULT_DAY_TYPE,
    day_part: int = DEFAULT_DAY_PART,
    project: str | None = "stl-datascience",
) -> pd.DataFrame:
    """Return annual headline metrics for segment_ids, with Medium/Heavy Duty truck metrics (md_*, hd_*) from ma_gt3."""
    ids = sorted({int(i) for i in segment_ids})
    empty_cols = [
        "segment_id",
        "year",
        "match_dir",
        "day_type",
        "day_part",
        "trips_volume",
        "avg_speed",
        "speed_percentiles",
        *[c for c, _ in CLASS_COLUMNS],
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
        class_select = ",\n          ".join(f"{c.split('_', 1)[0]}.{c}" for c, _ in CLASS_COLUMNS)
        sql = f"""
        WITH segs AS (
          SELECT segment_id FROM UNNEST({array_lit}) AS segment_id
        ),
        {_class_cte("md", WEIGHT_CLASSES["md"], array_lit, year_start, day_type, day_part)},
        {_class_cte("hd", WEIGHT_CLASSES["hd"], array_lit, year_start, day_type, day_part)}
        SELECT
          a.segment_id,
          CAST(a.year AS STRING) AS year,
          a.match_dir,
          a.day_type,
          a.day_part,
          a.trips_volume,
          a.avg_speed,
          a.speed_percentiles,
          {class_select}
        FROM `{MA_TT3}` a
        JOIN segs s USING (segment_id)
        LEFT JOIN md
          ON a.segment_id = md.segment_id
         AND a.year = md.year
         AND a.match_dir = md.match_dir
         AND a.day_type = md.day_type
         AND a.day_part = md.day_part
        LEFT JOIN hd
          ON a.segment_id = hd.segment_id
         AND a.year = hd.year
         AND a.match_dir = hd.match_dir
         AND a.day_type = hd.day_type
         AND a.day_part = hd.day_part
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
        scalar_cols = ["segment_id", "match_dir", "day_type", "day_part", "trips_volume", "avg_speed", *CLASS_SCALAR_COLUMNS]
        for col in scalar_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # bq JSON encodes arrays as lists already (NULL/empty -> None); coerce element types.
        def _to_list(cast):
            def conv(v):
                if v is None or (isinstance(v, float) and pd.isna(v)) or len(v) == 0:
                    return None
                return [cast(x) for x in v]

            return conv

        for col in ["speed_percentiles", *CLASS_INT_ARRAY_COLUMNS]:
            if col in df.columns:
                df[col] = df[col].apply(_to_list(int))
        for col in CLASS_ARRAY_COLUMNS:
            if col in df.columns and col not in CLASS_INT_ARRAY_COLUMNS:
                df[col] = df[col].apply(_to_list(float))
    logger.info("Fetched %s metric rows from BigQuery", len(df))
    return df
