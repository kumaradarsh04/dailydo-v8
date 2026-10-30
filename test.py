import os
import requests

from dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ.get("MODEL_API_KEY", "")
URL = "https://api.meta.ai/v1/responses"
def get_response(prompt: str, api_key: str, muse_url: str):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    request_body = {
            "model": "muse-spark-1.3-contributor",
            "input": [
                {"role": "system", "content": "You are a daily planner assistant. Give the tasks as if you are giving it to a absolute beginner. Always output valid JSON"},
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            ],
            "temperature": 0.4,
            # "responseMimeType": "application/json"
        }

    try:
        response = requests.post(
            muse_url,
            headers=headers,
            # json=payload,
            json=request_body,
        )
        print(response)
        response.raise_for_status()
        return response

    except Exception as e:
        print(f"The Error: {e}")
        return e
