"""FeatureInspire — web-search similar AI-agent projects and propose improvements.
Uses existing Gemini grounded search (same key as web_search.py).
No new dependencies; safe against quota errors (retries transient 429/503).
"""

import json
import time

from core.utils import get_base_dir

BASE_DIR = get_base_dir()
CONFIG = BASE_DIR / "config" / "api_keys.json"

TRANSIENT = ("429", "resource_exhausted", "quota", "rate limit", "500", "502", "503", "504")


def _key():
    with open(CONFIG) as f:
        return json.load(f)["gemini_api_key"]


def inspire(query="open source python desktop AI assistant agent features 2026", retries=2):
    from google import genai

    client = genai.Client(api_key=_key())
    for attempt in range(retries):
        try:
            r = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Find similar open-source AI desktop agent projects on GitHub. "
                f"List 3-5 notable repositories and 2-3 features they have "
                f"that EraOne (a Python desktop AI with voice/media/browser control) could adopt. "
                f"Search topic: {query}",
                config={"tools": [{"google_search": {}}]},
            )
            text = "".join(
                p.text for p in r.candidates[0].content.parts if hasattr(p, "text") and p.text
            )
            return {"ok": True, "inspiration": text[:4000], "source": "gemini_grounded_search"}
        except Exception as e:
            msg = str(e).lower()
            if any(m in msg for m in TRANSIENT) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            return {"ok": False, "error": str(e), "retryable": any(m in msg for m in TRANSIENT)}
    return {"ok": False, "error": "max retries"}
