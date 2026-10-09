"""Download OSM continent snapshots from the GCS map archive."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime
from pathlib import Path

from config import OSM_BUCKET, OSM_GCS_PROJECT

logger = logging.getLogger(__name__)

# Archive dirs look like 25-Jan-31 or 25-oct-01
_VINTAGE_RE = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{2}$")


def list_vintages(bucket: str = OSM_BUCKET) -> list[str]:
    """Return archive vintage prefixes newest-first via gsutil."""
    result = subprocess.run(
        ["gsutil", "ls", f"gs://{bucket}/"],
        check=True,
        capture_output=True,
        text=True,
    )
    prefixes: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip().rstrip("/")
        name = line.rsplit("/", 1)[-1]
        if _VINTAGE_RE.match(name):
            prefixes.append(name)

    def sort_key(v: str) -> tuple:
        for fmt in ("%y-%b-%d", "%y-%B-%d"):
            try:
                return (0, datetime.strptime(v, fmt))
            except ValueError:
                continue
        return (1, v)

    return sorted(set(prefixes), key=sort_key, reverse=True)


def resolve_vintage(vintage: str | None = None, *, bucket: str = OSM_BUCKET) -> str:
    if vintage:
        return vintage
    vintages = list_vintages(bucket=bucket)
    if not vintages:
        raise FileNotFoundError(
            f"No vintage prefixes found in gs://{bucket}/. Pass --vintage explicitly."
        )
    logger.info("Using newest archive vintage: %s", vintages[0])
    return vintages[0]


def download_source_pbf(
    vintage: str,
    source_region: str,
    dest_dir: Path,
    *,
    bucket: str = OSM_BUCKET,
    project: str | None = None,  # kept for CLI compat; gsutil uses ADC/gcloud auth
    force: bool = False,
) -> Path:
    """Download `{vintage}/{source_region}.osm.pbf` into dest_dir via gsutil."""
    del project  # unused; gsutil uses active gcloud credentials
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{source_region}.osm.pbf"
    dest = dest_dir / f"{vintage}_{filename}"
    if dest.exists() and dest.stat().st_size > 0 and not force:
        logger.info("Reusing cached PBF: %s", dest)
        return dest

    uri = f"gs://{bucket}/{vintage}/{filename}"
    logger.info("Downloading %s -> %s", uri, dest)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()
    subprocess.run(["gsutil", "-m", "cp", uri, str(tmp)], check=True)
    tmp.replace(dest)
    return dest
