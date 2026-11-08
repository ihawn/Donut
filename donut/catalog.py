"""Read the model catalog exposed by a configured compatible endpoint."""

import json
import os
from urllib.request import Request, urlopen
from .providers import validate


def endpoint_models(profile):
    validate(profile)
    key = os.environ.get(profile["key_env"])
    if not key:
        raise ValueError("Set " + profile["key_env"] + " to discover this connection’s models.")
    request = Request(profile["base_url"].rstrip("/") + "/models", headers={"Authorization": "Bearer " + key})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Provider returned an unsupported model catalog.")
    return [
        {"id": row["id"], "name": row["id"]}
        for row in payload["data"]
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
