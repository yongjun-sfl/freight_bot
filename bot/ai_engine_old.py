import os
import json
import sys
import logging
import re
import io
from google import genai
from google.genai import types
from PIL import Image

logger = logging.getLogger(__name__)

#ollama_client = ollama.Client(host=os.getenv('OLLAMA_HOST', 'http://ollama_ai:11434'))

# Initialize Gemini Client using your API key from environment
#client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Explicitly fetch API key from environment
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

def clean_text_locally(raw_text: str) -> dict:
    try:
        res = ollama_client.chat(
            model='qwen2.5:3b',
            messages=[
                {
                    'role': 'system', 
                    'content': (
                        "You are a strict logistics data extraction tool. "
                        "Identify the starting terminal/location (origin) and the ending terminal/location (destination). "
                        "Drivers use symbols like '=>', '->', 'to', or 'goes' to separate routes. "
                        "Example: '200 => SDS' means origin='200', destination='SDS'. "
                        "Example: 'empty 200 => 300' means origin='200', destination='300'. "
                        "Ignore filler text like 'testing bot' or 'empty'. "
                        "If a field is missing, use null."
                    )
                },
                {'role': 'user', 'content': raw_text}
            ],
            format=TripLogExtraction.model_json_schema()
        )
        return json.loads(res['message']['content'])
    except Exception:
        return {"origin": None, "destination": None, "time_info": None}



def resize_image_for_ollama(image_bytes: bytes, max_dim: int = 600) -> bytes:
    """Downscales image to 600px for high speed on CPU while preserving readable text."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=80)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Error resizing image: {e}")
        return image_bytes


def extract_bol_locally(image_bytes: bytes) -> dict:
    default_fallback = {
        "is_bol": False,
        "bol_number": None, 
        "trailer_number": None, 
        "shipper_signed": False, 
        "receiver_signed": False
    }

    if not GEMINI_KEY:
        logger.error("❌ GEMINI_API_KEY environment variable is missing!")
        return default_fallback

    client = genai.Client(api_key=GEMINI_KEY)

    prompt = """Analyze this logistics image carefully and extract key details into JSON:

1. "is_bol": true if paper shipping document (BOL, Delivery Order, packing list); false if truck or plug photo.
2. "bol_number": Extract the primary document tracking ID (Check Delivery #, DO#, BOL #, or Reserve No). Example: "BTPAC_279" or "8000222154".
3. "trailer_number": Extract trailer unit number if present (Example: "77274").
4. "shipper_signed": true if handwritten signature, checkmark, or date is under shipper/origin section.
5. "receiver_signed": true if signed/stamped under receiver section.

Return raw JSON only matching this structure:
{
  "is_bol": true,
  "bol_number": "string",
  "trailer_number": "string",
  "shipper_signed": true,
  "receiver_signed": false
}"""

    # Primary and fallback models
    models_to_try = ['gemini-3.5-flash', 'gemini-1.5-flash', 'gemini-2.5-flash']

    for model_name in models_to_try:
        # Retry up to 3 times per model for transient 503 errors
        for attempt in range(3):
            try:
                logger.info(f"⚡ Requesting extraction via {model_name} (Attempt {attempt + 1})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                        prompt
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.0
                    )
                )

                content = response.text.strip()
                logger.info(f"🔍 {model_name} Output: '{content}'")

                data = json.loads(content)
                b_num = str(data.get("bol_number")).strip() if data.get("bol_number") else None
                t_num = str(data.get("trailer_number")).strip() if data.get("trailer_number") else None

                if b_num in ["string", "null", "none"]: b_num = None
                if t_num in ["string", "null", "none"]: t_num = None

                return {
                    "is_bol": bool(data.get("is_bol", False)),
                    "bol_number": b_num,
                    "trailer_number": t_num,
                    "shipper_signed": bool(data.get("shipper_signed", False)),
                    "receiver_signed": bool(data.get("receiver_signed", False))
                }

            except Exception as e:
                err_str = str(e)
                if "503" in err_str or "UNAVAILABLE" in err_str:
                    logger.warning(f"⚠️ {model_name} busy (503). Retrying in 2s...")
                    time.sleep(2)
                else:
                    logger.error(f"❌ Gemini extraction error on {model_name}: {e}")
                    break  # Try next model in list

    return default_fallback



