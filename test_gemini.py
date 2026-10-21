"""Check your Gemini key, network and model name without printing the key.

Run from the specpay folder:
    py test_gemini.py                      (tests the model named in .env)
    py test_gemini.py gemini-2.5-flash     (tests a specific model)
"""
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("GEMINI_API_KEY")
if not key:
    raise SystemExit("GEMINI_API_KEY is missing from your .env file")
model = sys.argv[1] if len(sys.argv) > 1 else os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
base = "https://generativelanguage.googleapis.com/v1beta"
headers = {"x-goog-api-key": key}

print("Step 1: listing the models your key can use ...")
start = time.time()
try:
    r = httpx.get(f"{base}/models", headers=headers, params={"pageSize": 200}, timeout=20)
except httpx.HTTPError as exc:
    raise SystemExit(
        f"  Could not reach Google after {time.time() - start:.1f}s ({type(exc).__name__}).\n"
        "  This is a network problem: try another network or a mobile hotspot, and turn off any VPN."
    )
print(f"  Status {r.status_code} in {time.time() - start:.1f}s")
if r.status_code != 200:
    raise SystemExit("  " + r.text[:300])
names = [
    m["name"].split("/")[-1]
    for m in r.json().get("models", [])
    if "generateContent" in m.get("supportedGenerationMethods", [])
]
flash = [n for n in names if "flash" in n]
print("  Flash models available:", ", ".join(flash[:20]) or "none found")

print(f"Step 2: asking '{model}' a tiny question ...")
start = time.time()
try:
    r = httpx.post(
        f"{base}/models/{model}:generateContent",
        headers=headers,
        json={"contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}]},
        timeout=60,
    )
except httpx.HTTPError as exc:
    raise SystemExit(
        f"  Failed after {time.time() - start:.1f}s: {type(exc).__name__}.\n"
        "  Google is reachable but this model is not answering. Try another model from the list above."
    )
print(f"  Status {r.status_code} in {time.time() - start:.1f}s")
print("  " + r.text[:500].replace("\n", " "))
