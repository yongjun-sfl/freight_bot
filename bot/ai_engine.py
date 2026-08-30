import os
import io
import json
import time
import random
import asyncio
import logging
from PIL import Image
from pdf2image import convert_from_bytes
from google import genai
from google.genai import types

from config import TABLE_LOCATION_CODES

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
# Stable alias rather than a pinned point release. Measured 2026-08-28:
# gemini-3.5-flash-lite returned 503/timeout on 2 of 5 calls, while
# gemini-flash-lite-latest returned 200 on 5 of 5.
MODEL_NAME = "gemini-flash-lite-latest"

# Transient conditions worth waiting out. Anything else (400, 401, 404,
# malformed request) is a bug and must fail immediately rather than burn
# the retry budget.
# We never pass tools, so the SDK's automatic function calling is pure
# overhead on every request.
# maximum_remote_calls must be cleared too: leaving its default of 10 alongside
# disable=True makes the SDK warn on every single request.
NO_FUNCTION_CALLING = types.AutomaticFunctionCallingConfig(
    disable=True, maximum_remote_calls=None
)


def json_config(**kwargs) -> "types.GenerateContentConfig":
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.0,
        automatic_function_calling=NO_FUNCTION_CALLING,
        **kwargs,
    )


RETRYABLE_MARKERS = (
    "503", "UNAVAILABLE", "high demand",
    "429", "RESOURCE_EXHAUSTED",
    "500", "INTERNAL",
    "504", "DEADLINE_EXCEEDED", "timed out",
)

LOCATION_CACHE = {
    "codes": [],
    "alias_map": {},
    # canonical code -> physical site. 200F (front/inbound) and 200R
    # (rear/outbound) are distinct codes on a leg record but one place when
    # deciding whether a driver has returned and closed a round.
    "site_map": {},
    "official_name": {},
}


async def refresh_location_cache(pool):
    """Loads canonical location codes and aliases from MySQL into memory."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT canonical_code, aliases, site_code, official_name 
                      FROM {TABLE_LOCATION_CODES} 
                     WHERE is_active = TRUE;"""
            )
            rows = await cur.fetchall()
            
            codes = []
            alias_map = {}
            site_map = {}
            official = {}
            for code, aliases, site, official_name in rows:
                code_upper = code.strip().upper()
                codes.append(code_upper)
                alias_map[code_upper] = code_upper
                site_map[code_upper] = (site or code).strip().upper()
                if official_name:
                    official[code_upper] = official_name.strip()
                
                if aliases:
                    for alias in aliases.split(','):
                        clean_alias = alias.strip().upper()
                        if clean_alias:
                            alias_map[clean_alias] = code_upper
            
            LOCATION_CACHE["codes"] = list(set(codes))
            LOCATION_CACHE["alias_map"] = alias_map
            LOCATION_CACHE["site_map"] = site_map
            LOCATION_CACHE["official_name"] = official
            logger.info(f"Location cache refreshed: {len(codes)} valid codes loaded.")


def normalize_location(raw_loc: str) -> str:
    """Resolves driver text or alias directly to canonical MySQL location code."""
    if not raw_loc or raw_loc in ["UNKNOWN", "MISSING_ORIGIN", "MISSING_DEST", "NONE", "NULL"]:
        return "UNKNOWN"
    
    clean = raw_loc.strip().upper()
    return LOCATION_CACHE["alias_map"].get(clean, clean)


def site_of(location: str) -> str:
    """The physical site a code belongs to, for deciding round closure.

    200F and 200R are the same yard; unmapped codes are their own site.
    """
    if not location:
        return location
    clean = location.strip().upper()
    return LOCATION_CACHE["site_map"].get(clean, clean)


def convert_pdf_to_images(pdf_bytes: bytes) -> list[bytes]:
    try:
        images = convert_from_bytes(pdf_bytes, first_page=1, last_page=3)
        image_bytes_list = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            image_bytes_list.append(buf.getvalue())
        return image_bytes_list
    except Exception as e:
        logger.error(f"Failed to convert PDF to images: {e}")
        return []


def compress_image(image_bytes: bytes, max_dim: int = 1800) -> bytes:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"Image compression failed: {e}")
        return image_bytes


def call_gemini_with_retry(contents, config=None, retries=5, initial_delay=1.0, max_delay=8.0):
    """Call Gemini, waiting out transient failures.

    The budget is ~15s across 5 attempts (1+2+4+8, jittered). The previous
    3 attempts / ~3s was routinely shorter than an observed demand spike,
    and a lapsed budget means a driver's message is dropped entirely.
    """
    delay = initial_delay
    for attempt in range(1, retries + 1):
        try:
            return client.models.generate_content(
                model=MODEL_NAME,
                contents=contents,
                config=config
            )
        except Exception as e:
            err_msg = str(e)
            retryable = any(marker in err_msg for marker in RETRYABLE_MARKERS)
            if retryable and attempt < retries:
                # Jitter avoids a fleet of drivers retrying in lockstep.
                sleep_for = delay * (1.0 + random.random() * 0.25)
                logger.warning(
                    f"Gemini transient failure (attempt {attempt}/{retries}): {err_msg[:120]}. "
                    f"Retrying in {sleep_for:.1f}s..."
                )
                time.sleep(sleep_for)
                delay = min(delay * 2.0, max_delay)
            else:
                logger.error(f"Gemini API call failed after {attempt} attempt(s): {e}")
                raise


def parse_text_with_llm(text: str) -> dict:
    if not text or not text.strip():
        return {"case_type": "NONE_WORK_RELATED"}

    if not client:
        logger.error("GEMINI_API_KEY is not configured; driver text cannot be parsed.")
        return {
            "case_type": "PARSE_FAILED",
            "parse_error": "GEMINI_API_KEY is not configured",
        }

    if LOCATION_CACHE["codes"]:
        known_locations = ", ".join(LOCATION_CACHE["codes"])
        location_rule = f"KNOWN VALID CODES: [{known_locations}]"
    else:
        location_rule = "KNOWN VALID CODES: [Extract short alphanumeric location names dynamically]"
    
    prompt = f"""You are an expert logistics dispatch parser for short-haul inter-facility shuttle operations.
Analyze driver message: "{text}"

{location_rule}

DRIVER SLANG & PATTERN DICTIONARY:
- BOBTAIL SLANG: "bobtail", "bt", "b/t", "no trailer", "single tractor", "tractor only", "bob tail"
- EMPTY SLANG: "empty", "mt", "emp", "e/t"
- DEPARTURE SLANG: "heading to", "leaving", "outbound", "departed", "going to", "200 to e2f", "200 -> e2f"
- ARRIVAL SLANG: "arrived", "at door", "in yard", "gate", "here at", "in dock", "reached"

CASE CLASSIFICATION RULES:
1. "CASE_1_ORIGIN_DEPARTURE": Driver is reporting outbound movement, leaving a facility, or traveling between facilities (e.g., "leaving 200", "200 to E2F", "heading to E2F bt").
2. "CASE_2_DESTINATION_ARRIVAL": Driver is reporting arrival at a facility, gate, door, or yard (e.g., "arrived E2F", "at E2F door 45", "in yard at E2F").
3. "CASE_HISTORICAL_BOL_UPDATE": Document upload or message specifically referencing late paperwork, delivery receipts, or historical BOLs.
4. "CASE_3_INTRA_FACILITY_MOVE": Driver is repositioning a trailer WITHIN one facility -- between doors, or between a door and the yard. No facility-to-facility travel is involved. Examples:
   - "empty move #13 to #47"      -> origin_dock="13", destination_dock="47", load_status="EMPTY"
   - "loaded move #4 to #13"      -> origin_dock="4",  destination_dock="13", load_status="LOADED"
   - "empty dropped yard"         -> origin_dock=null, destination_dock="YARD", load_status="EMPTY"
   - "moved 44821 door 7 to yard" -> origin_dock="7",  destination_dock="YARD"
5. "CASE_CLOCK_IN": Driver is starting their shift. "clock in", "clocked in", "clocking in", "출근".
6. "CASE_CLOCK_OUT": Driver is ending their shift. "clock out", "clocked out", "clocking out", "퇴근".
7. "CASE_WORK_FINISHED": Driver reports a live load or unload is complete WITHOUT also announcing a departure. "live unloading finished", "unload done", "loading finished", "finished unloading".
8. "NONE_WORK_RELATED": Casual chat, non-shuttle messages, or non-logistics updates.

SEPARATE FLAG -- "work_finished":
- Set true whenever the message says a live load or unload has been completed, INCLUDING when the driver announces a departure in the same breath ("live loading finished load 200 to E2F"). In that case the case_type is still CASE_1_ORIGIN_DEPARTURE and work_finished is true; the completion belongs to the trip they are ending, the departure starts the next one.
- Set false otherwise.

EXTRACTION & NORMALIZATION RULES:
1. Location Extraction:
   - Match facility mentions to the KNOWN VALID CODES provided whenever possible.
   - If a driver uses a shorthand code (e.g., "200" for "200F"), extract the raw shorthand code (e.g., "200").
   - Extract origin_location and destination_location in uppercase (e.g., "load pickup 200 to e2f" -> origin_location="200", destination_location="E2F").

2. Door Numbers vs Locations:
   - Door, bay, or spot identifiers (starting with "#", "door", "bay", "spot") are NEVER locations. A facility is a code like "200", "E2F" or "SDS"; a door is a position inside one.
   - For CASE_1 and CASE_2 a single door goes in door_number (e.g. "#47" or "door 47" -> door_number="47").
   - For CASE_3_INTRA_FACILITY_MOVE there are two positions: put the one moved FROM in origin_dock and the one moved TO in destination_dock. Leave door_number null.
   - Strip the leading "#": "#13" -> "13".
   - When the driver names the yard, lot or parking area rather than a numbered door, use the literal string "YARD".
   - SDS uses a yard slot written "DO# 34", "DO 34" or "do34". Put just the number in do_number (e.g. "34"). It is a parking position, NOT a delivery order number from any paperwork, and no other site uses it.
   - origin_dock and destination_dock hold POSITIONS ONLY: a door number, or the literal "YARD". A facility code such as 200, 200R, E2F or SDS is NEVER a dock, however the driver phrases it.
   - If the driver names the facility on an internal move ("drop empty 200 r yard", "moved to yard at E2F"), put that facility in BOTH origin_location and destination_location -- the move begins and ends there -- and leave the dock fields for the positions only. Here "drop empty 200 r yard" means origin_location="200R", destination_location="200R", destination_dock="YARD", origin_dock=null.
   - If no facility is named, leave both location fields null; it is inferred from the driver's last known position.


3. Load Status & Trailer Details:
   - Set load_status to "BOBTAIL", "EMPTY", or "LOADED" based on text or slang terms above.
   - Extract trailer_number (e.g., "77344").

Return raw JSON ONLY:
{{
  "case_type": "CASE_1_ORIGIN_DEPARTURE" | "CASE_2_DESTINATION_ARRIVAL" | "CASE_HISTORICAL_BOL_UPDATE" | "CASE_3_INTRA_FACILITY_MOVE" | "CASE_CLOCK_IN" | "CASE_CLOCK_OUT" | "CASE_WORK_FINISHED" | "NONE_WORK_RELATED",
  "work_finished": boolean,
  "origin_location": string or null,
  "destination_location": string or null,
  "origin_dock": string or null,
  "destination_dock": string or null,
  "do_number": string or null,
  "trailer_number": string or null,
  "door_number": string or null,
  "action": "LIVE_UNLOAD" | "DROP_DOCK" | "DROP_YARD" | "DROP_DOOR" | "BOBTAIL_ARRIVE" | null,
  "load_status": "LOADED" | "EMPTY" | "BOBTAIL" | null
}}"""

    try:
        response = call_gemini_with_retry(
            contents=prompt,
            config=json_config()
        )
        return json.loads(response.text)
    except Exception as e:
        # Must NOT collapse to NONE_WORK_RELATED: that is indistinguishable
        # from casual chat, so the state machine silently discards the
        # message and the truck movement is never recorded.
        logger.error(f"Failed to parse text after retries: {e}")
        return {"case_type": "PARSE_FAILED", "parse_error": str(e)[:200]}


def extract_bol_locally(files: list[bytes]) -> dict:
    if not files or not client:
        return {}

    processed_images = []
    for file_bytes in files:
        if file_bytes.startswith(b"%PDF"):
            processed_images.extend(convert_pdf_to_images(file_bytes))
        else:
            processed_images.append(file_bytes)

    if not processed_images:
        return {}

    dynamic_vision_prompt = """Analyze this image.

1. "is_paper_document": Set to True ONLY if this image is a paper document (Bill of Lading, shipping paper, manifest, reservation instruction sheet, signature paper). Set to False if it is a photo of a trailer, truck, container body, or license plate.
2. "bol_number": Read the entire document semantically. Identify the primary tracking, BOL, delivery, reservation, or manifest number.
   - Look explicitly for terms like: "Reservation No.", "Reservation #", "Res #", "BOL", "Bill of Lading", "B/L", "Delivery #", "Shipment #", "DO #", "Ref #", "Tracking #".
3. "document_type":
   - Set to "RM" if the document explicitly contains "Reservation No.", "Reservation #", "Res #", "Reservation", or raw material component identifiers.
   - Set to "FG" if the document contains standard "Bill of Lading", "BOL #", "Delivery #", or customer finished goods shipment details.
4. "trailer_number": Search the document, door decals, or bumper prints for trailer or equipment identifiers (e.g., "77344").
4b. "do_number": ONLY if the letters "DO" or "D.O." literally appear next to a number, as in "DO# 34" or "D.O. 34". SDS clerks hand-write this yard slot so a driver can find a trailer in a large yard. Do NOT return a number that merely looks like a slot -- a bare handwritten "#47" is a dock, not a DO number. If the letters DO are not present, return null.

4c. "dock_number": a hand-written "#NN" with no other label is the dock the trailer was loaded at or delivered to. Return just the digits, e.g. "#47" -> "47". Return null if absent.

4d. "rm_seq": RM paperwork is hand-marked with the date and that load's sequence in the day's allocation, written "08/28-7" or "8/28 - 7", meaning the 7th RM load of 28 August. Return ONLY the number after the dash, e.g. "7". Return null if there is no such mark.
4e. "materials": the line items in the table under the MATERIAL / DESCRIPTION / QTY / WEIGHT / CONT NO / REMARK headings. Return one object per line, or an empty list if there is no such table. Copy values exactly as printed; do not normalise or infer.
   - "material_code": the MATERIAL value, e.g. "11800335"
   - "description": e.g. "GLASS"
   - "qty": the total quantity with its unit as printed, e.g. "10 PLT"
   - "weight": as printed, or null
   - "cont_no": the CONT NO column, or null
   - "batch_no": if the REMARK reads "Batch# 0001836335", return just "0001836335". This is issued separately by the receiving manager and is their reference, so it must not be left inside the remark text.
   - "remark": anything else in REMARK, or null

5. "shipper_signed": True only if the ORIGIN or SHIPPER side carries a hand-written signature, initials, or a department/company stamp authorising release (an "RM DEPT" stamp from the pick-up company counts).
6. "receiver_signed": True only if the DELIVERY or CONSIGNEE section specifically carries a hand-written signature or initials from whoever received the goods.
   - Be conservative. A stamp from the SHIPPING company, a date, a dock number, or any other handwriting elsewhere on the page is NOT a receiver signature.
   - Reporting a receipt that did not happen is far worse than missing one: it silently satisfies a compliance check that exists to catch missing proof of delivery. When unsure, return False.

Return raw JSON ONLY:
{
  "is_paper_document": boolean,
  "bol_number": string or null,
  "document_type": "FG" | "RM" | "UNKNOWN",
  "trailer_number": string or null,
  "do_number": string or null,
  "dock_number": string or null,
  "rm_seq": string or null,
  "materials": [
    {"material_code": string, "description": string, "qty": string,
     "weight": string or null, "cont_no": string or null,
     "batch_no": string or null, "remark": string or null}
  ],
  "shipper_signed": boolean,
  "receiver_signed": boolean
}"""

    result = {
        "bol_number": None,
        "document_type": "UNKNOWN",
        "trailer_number": None,
        "do_number": None,
        "dock_number": None,
        "rm_seq": None,
        "materials": [],
        "shipper_signed": False,
        "receiver_signed": False,
        "bol_image_blob": None,
        "is_paper_document": False,
        # True only when every image errored, i.e. the vision API is down.
        # Distinct from "scanned fine, found no document".
        "ocr_failed": False
    }

    scan_errors = 0
    for idx, img_bytes in enumerate(processed_images):
        compressed = compress_image(img_bytes, max_dim=1800)
        try:
            response = call_gemini_with_retry(
                contents=[
                    dynamic_vision_prompt,
                    types.Part.from_bytes(data=compressed, mime_type="image/jpeg")
                ],
                config=json_config()
            )
            data = json.loads(response.text)

            is_doc = data.get("is_paper_document", False)
            if is_doc:
                result["is_paper_document"] = True
                if not result["bol_image_blob"]:
                    result["bol_image_blob"] = img_bytes

            if data.get("bol_number") and not result["bol_number"]:
                result["bol_number"] = data["bol_number"]

            if data.get("document_type") and data["document_type"] != "UNKNOWN":
                result["document_type"] = data["document_type"]

            if data.get("trailer_number") and not result["trailer_number"]:
                result["trailer_number"] = data["trailer_number"]

            for field in ("do_number", "dock_number", "rm_seq"):
                if data.get(field) and not result[field]:
                    result[field] = data[field]

            if data.get("materials") and not result["materials"]:
                result["materials"] = [m for m in data["materials"] if isinstance(m, dict)]

            if data.get("shipper_signed"):
                result["shipper_signed"] = True

            if data.get("receiver_signed"):
                result["receiver_signed"] = True

            # EARLY EXIT SHORT-CIRCUIT: Stop parsing remaining images once document details are acquired
            if result["bol_number"] and result["trailer_number"] and result["trailer_number"] != "UNKNOWN":
                logger.info(f"⚡ Early exit triggered on image #{idx+1}: Found BOL '{result['bol_number']}' & Trailer '{result['trailer_number']}'. Skipping remaining images.")
                break

        except Exception as e:
            scan_errors += 1
            logger.warning(f"Vision OCR scan error on image {idx+1}: {e}")

    result["ocr_failed"] = scan_errors == len(processed_images)
    return result


async def prepare_text_intent(text: str) -> dict:
    # parse_text_with_llm is blocking and now waits out transient failures for
    # up to ~15s. Called inline it would stall the whole bot for every driver.
    loop = asyncio.get_running_loop()
    llm_parsed = await loop.run_in_executor(None, parse_text_with_llm, text)
    return {
        "case_type": llm_parsed.get("case_type", "NONE_WORK_RELATED"),
        "parse_error": llm_parsed.get("parse_error"),
        "raw_text": text or "",
        "text_trailer": llm_parsed.get("trailer_number"),
        "ocr_trailer": None,
        "bol_number": None,
        "document_type": "UNKNOWN",
        "origin_location": llm_parsed.get("origin_location"),
        "destination_location": llm_parsed.get("destination_location"),
        "door_number": llm_parsed.get("door_number"),
        "origin_dock": llm_parsed.get("origin_dock"),
        "destination_dock": llm_parsed.get("destination_dock"),
        "do_number": llm_parsed.get("do_number"),
        "rm_seq": None,
        "materials": [],
        "work_finished": bool(llm_parsed.get("work_finished")),
        "action": llm_parsed.get("action"),
        "load_status": llm_parsed.get("load_status"),
        "shipper_signed": False,
        "receiver_signed": False,
        "primary_image_blob": None
    }


async def prepare_image_intent(images: list[bytes], caption_text: str, loop) -> dict:
    has_caption = bool(caption_text and caption_text.strip())

    # Both calls are blocking; neither may run on the event loop.
    llm_parsed = (
        await loop.run_in_executor(None, parse_text_with_llm, caption_text)
        if has_caption else {}
    )
    ocr_data = await loop.run_in_executor(None, extract_bol_locally, images)

    if has_caption:
        case_type = llm_parsed.get("case_type", "CASE_1_ORIGIN_DEPARTURE")
    elif ocr_data.get("ocr_failed"):
        # Vision was unreachable for every image. Do not pretend the driver
        # posted something irrelevant -- surface it instead.
        case_type = "PARSE_FAILED"
    else:
        case_type = "CASE_AUTO_RESOLVE" if ocr_data.get("is_paper_document") else "NONE_WORK_RELATED"

    return { 
        "case_type": case_type,
        "parse_error": llm_parsed.get("parse_error") or (
            "Vision OCR failed on every attached image" if ocr_data.get("ocr_failed") else None
        ),
        "raw_text": caption_text or "",
        "text_trailer": llm_parsed.get("trailer_number"),
        "ocr_trailer": ocr_data.get("trailer_number"),
        "bol_number": ocr_data.get("bol_number"),
        "document_type": ocr_data.get("document_type", "UNKNOWN"),
        "origin_location": llm_parsed.get("origin_location"),
        "destination_location": llm_parsed.get("destination_location"),
        "door_number": llm_parsed.get("door_number"),
        "origin_dock": llm_parsed.get("origin_dock"),
        "destination_dock": llm_parsed.get("destination_dock"),
        # Handwritten on the BOL by the SDS clerk, so the image is the more
        # reliable source; a caption mentioning it still wins if present.
        "do_number": llm_parsed.get("do_number") or ocr_data.get("do_number"),
        "rm_seq": ocr_data.get("rm_seq"),
        "materials": ocr_data.get("materials") or [],
        "work_finished": bool(llm_parsed.get("work_finished")),
        "action": llm_parsed.get("action"),
        "load_status": llm_parsed.get("load_status"),
        "shipper_signed": ocr_data.get("shipper_signed", False),
        "receiver_signed": ocr_data.get("receiver_signed", False),
        "primary_image_blob": ocr_data.get("bol_image_blob")
    }