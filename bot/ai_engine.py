import os, json, logging, ollama
from schemas import TripLogExtraction, BolVisionExtraction

ollama_client = ollama.Client(host=os.getenv('OLLAMA_HOST', 'http://ollama_ai:11434'))

def clean_text_locally(raw_text: str) -> dict:
    try:
        res = ollama_client.chat(
            model='qwen2.5:3b',
            messages=[{'role': 'system', 'content': 'Extract location details.'},
                      {'role': 'user', 'content': raw_text}],
            format=TripLogExtraction.model_json_schema()
        )
        return json.loads(res['message']['content'])
    except Exception:
        return {"origin": None, "destination": None, "time_info": None}

def extract_bol_locally(image_bytes: bytes) -> dict:
    """Runs completely locally on your EC2 instance using Llama 3.2 Vision."""
    try:
        res = ollama_client.chat(
            model='llama3.2-vision:11b',
            messages=[{
                'role': 'user',
                'content': 'Analyze this document image. Return the BOL ID number, Trailer number, and check if shipper or receiver signed it.',
                'images': [image_bytes]
            }],
            format=BolVisionExtraction.model_json_schema()
        )
        return json.loads(res['message']['content'])
    except Exception as e:
        logging.error(f"Local vision pipeline failed: {e}")
        return {"bol_number": None, "trailer_number": None, "shipper_signed": False, "receiver_signed": False}
