import re
import logging

from ai_engine import parse_text_with_llm

logger = logging.getLogger(__name__)


def parse_shuttle_chat(text: str) -> dict:
    if not text:
        return {}

    # Primary: Send raw driver text through Gemini 3.5 Flash
    llm_result = parse_text_with_llm(text)
    
    # If Gemini returns a valid scenario case type, use it
    if llm_result.get("case_type"):
        return llm_result

    # Fallback: Basic regex if LLM API call fails or is unreachable
    clean = text.strip().lower()
    route_match = re.search(r'(?:from\s+)?([a-z0-9_]+)\s+(?:to|->|➔)\s+([a-z0-9_]+)', clean)
    extracted_origin = route_match.group(1).upper() if route_match else "Origin"
    extracted_dest = route_match.group(2).upper() if route_match else "Destination"

    trailer_match = re.search(r'\b(\d{3,6})\b', text)
    trailer_number = trailer_match.group(1) if trailer_match else None

    return {
        "case_type": "CASE_1_ORIGIN_DEPARTURE",
        "origin_location": extracted_origin,
        "destination_location": extracted_dest,
        "trailer_number": trailer_number,
        "door_number": None,
        "load_status": "LOADED",
        "is_bobtail": False,
        "action": "PICKUP"
    }

async def infer_arrival_location(pool, user_id: int, trailer_number: str) -> dict:
    """
    Infers missing facility (Origin vs Destination) using active or completed leg history.
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # Check for active leg
            sql_active = """
                SELECT id, origin_location 
                FROM shuttle_legs 
                WHERE user_id = %s AND (trailer_number = %s OR trailer_number = 'BOBTAIL') AND leg_status = 'IN_TRANSIT'
                ORDER BY id DESC LIMIT 1;
            """
            await cur.execute(sql_active, (user_id, trailer_number))
            active_leg = await cur.fetchone()

            if active_leg:
                leg_id, origin = active_leg
                inferred_dest = "Destination" if origin == "Origin" else "Origin"
                return {"leg_id": leg_id, "inferred_location": inferred_dest, "is_active_leg": True}

            # Check driver's last completed trip
            sql_last = "SELECT destination_location FROM shuttle_legs WHERE user_id = %s ORDER BY id DESC LIMIT 1;"
            await cur.execute(sql_last, (user_id,))
            last_leg = await cur.fetchone()

            if last_leg and last_leg[0] == "Destination":
                return {"leg_id": None, "inferred_location": "Origin", "is_active_leg": False}
            
            return {"leg_id": None, "inferred_location": "Destination", "is_active_leg": False}