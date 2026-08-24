import os
import io
import json
import time
import logging
from PIL import Image
from pdf2image import convert_from_bytes
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = "gemini-3.5-flash-lite"

LOCATION_CACHE = {
    "codes": [],
    "alias_map": {}
}


async def refresh_location_cache(pool):
    """Loads canonical location codes and aliases from MySQL into memory."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT canonical_code, aliases FROM location_codes WHERE is_active = TRUE;")
            rows = await cur.fetchall()
            
            codes = []
            alias_map = {}
            for code, aliases in rows:
                code_upper = code.strip().upper()
                codes.append(code_upper)
                alias_map[code_upper] = code_upper  # Self-map
                
                if aliases:
                    for alias in aliases.split(','):
                        clean_alias = alias.strip().upper()
                        if clean_alias:
                            alias_map[clean_alias] = code_upper
            
            LOCATION_CACHE["codes"] = list(set(codes))
            LOCATION_CACHE["alias_map"] = alias_map
            logger.info(f"Location cache refreshed: {len(codes)} valid codes loaded.")


def normalize_location(raw_loc: str) -> str:
    """Resolves driver text or alias directly to the canonical MySQL location code."""
    if not raw_loc or raw_loc in ["UNKNOWN", "MISSING_ORIGIN", "MISSING_DEST", "NONE", "NULL"]:
        return "UNKNOWN"
    
    clean = raw_loc.strip().upper()
    return LOCATION_CACHE["alias_map"].get(clean, clean)


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
        logger.warning(f"Image compression failed, fallback to raw: {e}")
        return image_bytes


def call_gemini_with_retry(contents, config=None, retries=3, initial_delay=1.0):
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
            if ("503" in err_msg or "UNAVAILABLE" in err_msg or "high demand" in err_msg) and attempt < retries:
                logger.warning(f"Gemini API 503 spike (Attempt {attempt}/{retries}). Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2.0
            else:
                logger.error(f"Gemini API call failed after {attempt} attempt(s): {e}")
                raise e


def parse_text_with_llm(text: str) -> dict:
    if not text or not text.strip() or not client:
        return {"case_type": "NONE_WORK_RELATED"}

    if LOCATION_CACHE["codes"]:
        known_locations = ", ".join(LOCATION_CACHE["codes"])
        location_rule = f"KNOWN VALID CODES: [{known_locations}]"
    else:
        location_rule = "KNOWN VALID CODES: [Extract short alphanumeric location names dynamically]"

    prompt = f"""You are an expert logistics dispatch parser for short-haul shuttle operations.
Analyze this driver message (English or Korean):
"{text}"

Task: Extract state transition parameters dynamically.

{location_rule}

RULES:
1. Location Extraction:
   - Match facility mentions to the KNOWN VALID CODES provided whenever possible.
   - If a driver uses a shorthand code (e.g., "200" for "200F"), extract the raw shorthand code (e.g., "200").
   - Extract origin and destination in uppercase (e.g., "load pickup 200 to e2f" -> origin_location="200", destination_location="E2F").

2. Door Numbers vs Locations:
   - Door, bay, or spot identifiers (starting with "#", "door", "bay", "spot") belong in door_number (e.g., "#47" -> door_number="47").

3. Classification Rules:
   - "CASE_1_ORIGIN_DEPARTURE": Outbound departures, pickups, or movements between distinct facility codes.
   - "CASE_2_DESTINATION_ARRIVAL": In-facility arrivals, dock door assignments, live unloading/loading, or same-site dock spot moves.
   - "CASE_HISTORICAL_BOL_UPDATE": Document uploads with explicit text context regarding paper BOLs, signed receipts, or historical paperwork.
   - "NONE_WORK_RELATED": Casual chat or non-operational messages.

Return raw JSON matching this schema ONLY:
{{
  "case_type": "CASE_1_ORIGIN_DEPARTURE" | "CASE_2_DESTINATION_ARRIVAL" | "CASE_HISTORICAL_BOL_UPDATE" | "NONE_WORK_RELATED",
  "origin_location": string or null,
  "destination_location": string or null,
  "trailer_number": string or null,
  "door_number": string or null,
  "action": "UNLOAD_COMPLETED" | "LIVE_UNLOAD" | "LIVE_LOAD" | "DROP" | "HOOK" | null,
  "load_status": "LOADED" | "EMPTY" | "BOBTAIL"
}}"""

    try:
        response = call_gemini_with_retry(
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0)
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Failed to parse text with Gemini LLM: {e}")
        return {"case_type": "NONE_WORK_RELATED"}


def extract_bol_locally(files: list[bytes]) -> dict:
    """Scans upload batch with short-circuiting to minimize vision API usage."""
    if not files or not client:
        return {}

    processed_images = []
    for file_bytes in files:
        if file_bytes.startswith(b"%PDF"):
            pdf_imgs = convert_pdf_to_images(file_bytes)
            processed_images.extend(pdf_imgs)
        else:
            processed_images.append(file_bytes)

    if not processed_images:
        return {}

    dynamic_vision_prompt = """Analyze this image.

1. "is_paper_document": Set to True ONLY if this image is a paper document (Bill of Lading, shipping paper, manifest, reservation instruction sheet, signature paper). Set to False if it is a photo of a trailer, truck, container body, or license plate.
2. "bol_number": Read the entire document semantically. Identify the primary tracking, BOL, delivery, reservation, or manifest number.
   - Look explicitly for terms like: "Reservation No.", "Reservation #", "BOL", "Bill of Lading", "B/L", "Delivery #", "Shipment #", "DO #", "Ref #", "Tracking #".
3. "trailer_number": Search the document, door decals, or bumper prints for trailer or equipment identifiers (e.g., "77344").
4. "shipper_signed": Set to True if there is a signature, initials, or stamp in the origin/shipper/carrier section.
5. "receiver_signed": Set to True if there is a signature, stamp, checkmark, or handwritten note in the delivery/consignee section.

Return raw JSON matching this schema ONLY:
{
  "is_paper_document": boolean,
  "bol_number": string or null,
  "trailer_number": string or null,
  "shipper_signed": boolean,
  "receiver_signed": boolean
}"""

    result = {
        "bol_number": None,
        "trailer_number": None,
        "shipper_signed": False,
        "receiver_signed": False,
        "bol_image_blob": None,
        "is_paper_document": False
    }

    for idx, img_bytes in enumerate(processed_images):
        compressed = compress_image(img_bytes, max_dim=1800)
        try:
            response = call_gemini_with_retry(
                contents=[
                    dynamic_vision_prompt, 
                    types.Part.from_bytes(data=compressed, mime_type="image/jpeg")
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", 
                    temperature=0.0
                )
            )
            data = json.loads(response.text)
            logger.info(f"Vision OCR scan image #{idx+1}: {data}")

            is_doc = data.get("is_paper_document", False)
            if is_doc:
                result["is_paper_document"] = True

            if is_doc and not result["bol_image_blob"]:
                result["bol_image_blob"] = img_bytes

            if data.get("bol_number") and not result["bol_number"]:
                result["bol_number"] = data["bol_number"]

            if data.get("trailer_number") and not result["trailer_number"]:
                result["trailer_number"] = data["trailer_number"]

            if data.get("shipper_signed"):
                result["shipper_signed"] = True

            if data.get("receiver_signed"):
                result["receiver_signed"] = True

            # EARLY EXIT SHORT-CIRCUIT: Stop parsing remaining images once document details are acquired
            if result["bol_number"] and result["trailer_number"] and result["trailer_number"] != "UNKNOWN":
                logger.info(f"⚡ Early exit triggered on image #{idx+1}: Found BOL '{result['bol_number']}' & Trailer '{result['trailer_number']}'. Skipping remaining images.")
                break

        except Exception as e:
            logger.warning(f"Failed dynamic OCR scan on image {idx+1}: {e}")

    return result


async def prepare_text_intent(text: str) -> dict:
    llm_parsed = parse_text_with_llm(text)
    return {
        "case_type": llm_parsed.get("case_type", "NONE_WORK_RELATED"),
        "raw_text": text or "",
        "text_trailer": llm_parsed.get("trailer_number"),
        "ocr_trailer": None,
        "bol_number": None,
        "origin_location": llm_parsed.get("origin_location"),
        "destination_location": llm_parsed.get("destination_location"),
        "door_number": llm_parsed.get("door_number"),
        "action": llm_parsed.get("action"),
        "load_status": llm_parsed.get("load_status"),
        "shipper_signed": False,
        "receiver_signed": False,
        "primary_image_blob": None
    }


async def prepare_image_intent(images: list[bytes], caption_text: str, loop) -> dict:
    llm_parsed = parse_text_with_llm(caption_text) if caption_text and caption_text.strip() else {}
    ocr_data = await loop.run_in_executor(None, extract_bol_locally, images)

    # Route explicitly to CASE_AUTO_RESOLVE if an image is uploaded without text
    if caption_text and caption_text.strip():
        case_type = llm_parsed.get("case_type", "CASE_1_ORIGIN_DEPARTURE")
    else:
        case_type = "CASE_AUTO_RESOLVE" if ocr_data.get("is_paper_document") else "NONE_WORK_RELATED"

    return {
        "case_type": case_type,
        "raw_text": caption_text or "",
        "text_trailer": llm_parsed.get("trailer_number"),
        "ocr_trailer": ocr_data.get("trailer_number"),
        "bol_number": ocr_data.get("bol_number"),
        "origin_location": llm_parsed.get("origin_location"),
        "destination_location": llm_parsed.get("destination_location"),
        "door_number": llm_parsed.get("door_number"),
        "action": llm_parsed.get("action"),
        "load_status": llm_parsed.get("load_status"),
        "shipper_signed": ocr_data.get("shipper_signed", False),
        "receiver_signed": ocr_data.get("receiver_signed", False),
        "primary_image_blob": ocr_data.get("bol_image_blob")
    }