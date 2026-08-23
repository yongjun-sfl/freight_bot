import os
import json
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Instantiate modern Client (Do NOT use genai.configure)
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def parse_text_with_llm(text: str) -> dict:
    """Parses English and Korean driver updates for movements or historical BOL reconciliations."""
    if not text or not text.strip() or not client:
        return {"case_type": "NONE_WORK_RELATED"}

# --- INSIDE parse_text_with_llm (bot/ai_engine.py) ---

    prompt = f"""You are a logistics dispatch parser. Analyze this driver update (English or Korean):
"{text}"

Rules:
1. IF casual chit-chat or unrelated, return "case_type": "NONE_WORK_RELATED".
2. IF driver mentions uploading yesterday's/signed/past BOL (e.g., "yesterday BOL", "어제 서명", "지난거 BOL", "reconcile"), return "case_type": "CASE_HISTORICAL_BOL_UPDATE".
3. IF active movement between facilities, classify "CASE_1_ORIGIN_DEPARTURE" or "CASE_2_DESTINATION_ARRIVAL".
   - Carefully identify ORIGIN and DESTINATION based on prepositions or context.
   - Example: "to SDS from 200" means origin_location="200" and destination_location="SDS".
   - Example: "200 to SDS" means origin_location="200" and destination_location="SDS".
   - Example: "200에서 SDS로" means origin_location="200" and destination_location="SDS".
   - Set missing/unspecified locations to "INFER_FROM_HISTORY".

Return raw JSON matching this schema ONLY:
{{
  "case_type": "CASE_1_ORIGIN_DEPARTURE" | "CASE_2_DESTINATION_ARRIVAL" | "CASE_HISTORICAL_BOL_UPDATE" | "NONE_WORK_RELATED",
  "origin_location": "200" | "INFER_FROM_HISTORY",
  "destination_location": "SDS" | "INFER_FROM_HISTORY",
  "trailer_number": "77274" | null,
  "load_status": "LOADED" | "EMPTY"
}}"""

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Failed to parse text with Gemini LLM: {e}")
        return {"case_type": "NONE_WORK_RELATED"}


def extract_bol_locally(images: list[bytes]) -> dict:
    """Extracts Delivery #, BOL #, or Reserve No and trailer details from images using google-genai SDK."""
    if not images or not client:
        return {}

    prompt = """Analyze the provided image(s) from a logistics driver.
Identify:
1. "bol_number": Look specifically at the top right document header box for "Delivery #", "Delivery No.", "BOL #", "Bill of Lading", or "Reserve No" (e.g., "BTPAC_270"). Do NOT pick the DO# from the bottom table unless no top header number exists.
2. "trailer_number": Trailer/Container/Unit ID printed on paper or door/body (e.g., "77274").
3. "shipper_signed": True if shipper/origin signature or check mark is present.
4. "receiver_signed": True if receiver/consignee signature, stamp, or date check is present.

Return raw JSON matching this schema ONLY:
{
  "bol_number": string or null,
  "trailer_number": string or null,
  "shipper_signed": boolean,
  "receiver_signed": boolean
}"""

    contents = [prompt]
    for img_bytes in images:
        contents.append(
            types.Part.from_bytes(
                data=img_bytes,
                mime_type="image/jpeg",
            )
        )

    try:
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        return json.loads(response.text)
    except Exception as e:
        logger.error(f"Failed to extract document details via Gemini Vision: {e}")
        return {
            "bol_number": None,
            "trailer_number": None,
            "shipper_signed": False,
            "receiver_signed": False
        }