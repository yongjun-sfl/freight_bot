"""Driver manifests: the hand-filled sheet uploaded at the end of a shift.

Two jobs, deliberately separated.

The image is the record. Telegram is not an archive -- history can be lost,
and these sheets are the driver's own account of their day -- so the photo is
stored first and everything else is best effort on top of it.

The parsed rows are a hint, not a fact. Handwriting reads badly: on a real
manifest the trailer column came back as "11" for eight of nine rows because
the driver used ditto marks, and the model reported every one as confident.
So rows are validated against things that can actually be checked, kept apart
from shuttle_legs, and used for reconciliation rather than as a source.
"""

import hashlib
import json
import logging
import re

from ai_engine import (LOCATION_CACHE, call_gemini_with_retry, compress_image,
                       json_config, normalize_location)
from config import TABLE_MANIFEST_ROWS, TABLE_MANIFESTS

logger = logging.getLogger(__name__)

MANIFEST_PROMPT = """This is a photograph of a hand-filled SFL SHUTTLE DRIVER MANIFEST.
The sheet may be rotated; read it whichever way up it makes sense.

Return raw JSON only:
{
  "is_manifest": boolean,
  "driver_name": string or null,
  "manifest_date": string or null,
  "team": string or null,
  "rows": [
    {"origin": string or null, "depart_time": string or null,
     "destination": string or null, "arrive_time": string or null,
     "load_status": "LOADED"|"EMPTY"|null, "trailer_number": string or null}
  ]
}

Copy values exactly as written; do not tidy or infer them.
Where a cell repeats the row above using a ditto mark, a quote sign or a
vertical line, return null rather than guessing what it stands for. Return
null for anything illegible."""

TRAILER_RE = re.compile(r"^[A-Z0-9]{4,10}$")


def image_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def looks_like_manifest(ocr: dict) -> bool:
    """Whether a scanned page is a manifest rather than a BOL."""
    return bool(ocr.get("is_manifest"))


def read_manifest(image: bytes) -> dict:
    """Parse a manifest photo. Returns {} when it cannot be read."""
    try:
        response = call_gemini_with_retry(
            contents=[MANIFEST_PROMPT, _part(compress_image(image, max_dim=2000))],
            config=json_config(),
        )
        return json.loads(response.text)
    except Exception as e:
        logger.warning(f"Manifest OCR failed: {e}")
        return {}


def _part(data: bytes):
    from google.genai import types
    return types.Part.from_bytes(data=data, mime_type="image/jpeg")


def validate_row(row: dict, previous: dict = None) -> list:
    """Reasons this row should not be trusted.

    The model's own confidence is useless -- it reported every row of a real
    manifest as confident, including eight where the trailer was a misread
    ditto mark. These check against things that are actually knowable.
    """
    problems = []
    known = set(LOCATION_CACHE.get("codes") or [])
    known |= set(LOCATION_CACHE.get("alias_map") or {})

    for field in ("origin", "destination"):
        value = (row.get(field) or "").strip().upper()
        if not value:
            problems.append(f"{field} missing")
        elif value not in known and normalize_location(value) not in known:
            problems.append(f"{field} {value!r} is not a known facility")

    trailer = (row.get("trailer_number") or "").strip().upper()
    if trailer and not TRAILER_RE.match(trailer):
        problems.append(f"trailer {trailer!r} is implausible")

    if row.get("depart_time") and row.get("arrive_time"):
        if _minutes(row["arrive_time"]) is None or _minutes(row["depart_time"]) is None:
            problems.append("unreadable times")

    return problems


def _minutes(value):
    match = re.match(r"^\s*(\d{1,2}):(\d{2})", str(value or ""))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour * 60 + minute if 0 <= hour <= 23 and minute < 60 else None


async def store_manifest(cur, user_id, driver_name, received_at, image,
                         parsed: dict):
    """Save the photo and any rows read from it. Returns (id, stored_rows).

    Keyed on the image digest, so a driver resending the same photo does not
    create a second manifest.
    """
    digest = image_digest(image)
    await cur.execute(
        f"""INSERT INTO {TABLE_MANIFESTS}
                (user_id, driver_name, manifest_date, team, image_sha256,
                 image, received_at, row_count, flagged_rows)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id);""",
        (user_id, parsed.get("driver_name") or driver_name,
         parsed.get("manifest_date"), parsed.get("team"), digest, image,
         received_at, 0, 0),
    )
    await cur.execute(
        f"SELECT id FROM {TABLE_MANIFESTS} WHERE image_sha256 = %s;", (digest,))
    row = await cur.fetchone()
    if not row:
        return None, 0
    manifest_id = row[0]

    await cur.execute(
        f"SELECT COUNT(*) FROM {TABLE_MANIFEST_ROWS} WHERE manifest_id = %s;",
        (manifest_id,))
    (already,) = await cur.fetchone()
    if already:
        return manifest_id, 0

    rows = parsed.get("rows") or []
    flagged = 0
    for index, row_data in enumerate(rows, 1):
        problems = validate_row(row_data)
        flagged += bool(problems)
        await cur.execute(
            f"""INSERT INTO {TABLE_MANIFEST_ROWS}
                    (manifest_id, row_no, origin, depart_time, destination,
                     arrive_time, load_status, trailer_number, problems)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);""",
            (manifest_id, index, row_data.get("origin"),
             row_data.get("depart_time"), row_data.get("destination"),
             row_data.get("arrive_time"), row_data.get("load_status"),
             row_data.get("trailer_number"),
             "; ".join(problems) if problems else None),
        )
    await cur.execute(
        f"""UPDATE {TABLE_MANIFESTS} SET row_count = %s, flagged_rows = %s
             WHERE id = %s;""",
        (len(rows), flagged, manifest_id))
    return manifest_id, len(rows)
