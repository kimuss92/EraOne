from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request


def analyze_image_with_openai(
    image_bytes: bytes,
    prompt: str,
    api_key: str,
    model: str,
    timeout_s: int,
) -> str:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:image/jpeg;base64,{b64_image}"

    payload = json.dumps(
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url="https://api.openai.com/v1/responses",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=timeout_s)
        data = json.loads(resp.read().decode("utf-8"))
        output_text = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        output_text += content.get("text", "")
        return output_text
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI vision HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI vision request failed: {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"OpenAI vision failed: {e}") from e
