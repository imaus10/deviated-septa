import json
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def get_client() -> httpx.Client:
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    return httpx.Client(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers=headers,
        timeout=60,
    )


class DatetimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def json_dumps(obj):
    return json.dumps(obj, cls=DatetimeEncoder)
