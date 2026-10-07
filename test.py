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

    # payload = {
    #     "model": "muse-spark-1.3-contributor",
    #     "input": [{
    #         "role": "user",
    #         "content": [{"type": "input_text", "text": prompt}]
    #     }],
    #     "stream": False,
    #     # "max_output_tokens": 1000,
    # }
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

        response.raise_for_status()
        return response

    except Exception as e:
        # print(e)
        return e
    
if __name__ == "__main__":
    data = get_response(
        "What is the full form of AI? only full form",
        API_KEY, URL
    )
    # print(response)

    if data:
        data = data.json()

    result_output = data.get("output", [])
    if result_output:
        print(result_output[1]["content"][0]["text"])