"""Check your Groq key, network and model name without printing the key.

Run from the specpay folder:
    py test_groq.py                          (tests the model named in .env)
    py test_groq.py openai/gpt-oss-120b  (tests a specific model)
"""
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("GROQ_API_KEY")
if not key:
    raise SystemExit("GROQ_API_KEY is missing from your .env file")
model = sys.argv[1] if len(sys.argv) > 1 else os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
base = "https://api.groq.com/openai/v1"
headers = {"Authorization": f"Bearer {key}"}

print("Step 1: listing the models your key can use ...")
start = time.time()
try:
    r = httpx.get(f"{base}/models", headers=headers, timeout=20)
except httpx.HTTPError as exc:
    raise SystemExit(
        f"  Could not reach Groq after {time.time() - start:.1f}s ({type(exc).__name__}).\n"
        "  This is a network problem: try another network or a mobile hotspot, and turn off any VPN."
    )
print(f"  Status {r.status_code} in {time.time() - start:.1f}s")
if r.status_code != 200:
    raise SystemExit("  " + r.text[:300])
names = sorted(m["id"] for m in r.json().get("data", []))
print("  Models available:", ", ".join(names[:25]) or "none found")

print(f"Step 2: asking '{model}' a tiny question ...")
start = time.time()
try:
    r = httpx.post(
        f"{base}/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "Reply with the single word OK."}]},
        timeout=60,
    )
except httpx.HTTPError as exc:
    raise SystemExit(f"  Failed after {time.time() - start:.1f}s: {type(exc).__name__}")
print(f"  Status {r.status_code} in {time.time() - start:.1f}s")
print("  " + r.text[:400].replace("\n", " "))
