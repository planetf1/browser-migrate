
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validate a Safari History.json file against Apple'sValidate a Safari History.json file against Apple's import schema.

Usage:
    python validator.py /path/to/History.json
"""

import json
import sys
from typing import Any, Dict


REQUIRED_METADATA_KEYS = {
    "browser_name": str,
    "browser_version": str,
    "data_type": str,
    "export_time_usec": int,
    "schema_version": int,
}


def fail(msg: str) -> None:
    print(f"❌ {msg}")
    sys.exit(1)


def validate_metadata(md: Dict[str, Any]) -> None:
    for key, typ in REQUIRED_METADATA_KEYS.items():
        if key not in md:
            fail(f"Missing metadata field: {key}")
        if not isinstance(md[key], typ):
            fail(f"metadata.{key} must be {typ.__name__} (got {type(md[key]).__name__})")

    if md["data_type"] != "history":
        fail("metadata.data_type must be 'history'")

    if md["export_time_usec"] <= 0:
        fail("metadata.export_time_usec must be a positive UNIX microsecond timestamp")


def validate_item(item: Dict[str, Any], idx: int) -> None:
    # url
    if "url" not in item or not isinstance(item["url"], str) or not item["url"]:
        fail(f"item {idx}: 'url' missing or not a non-empty string")

    # time_usec
    if "time_usec" not in item or not isinstance(item["time_usec"], int):
        fail(f"item {idx}: 'time_usec' missing or not an integer")
    if item["time_usec"] <= 0:
        fail(f"item {idx}: 'time_usec' must be a positive UNIX microsecond timestamp")

    # visits_count
    if "visits_count" not in item or not isinstance(item["visits_count"], int):
        fail(f"item {idx}: 'visits_count' missing or not an integer")
    if item["visits_count"] < 1:
        fail(f"item {idx}: 'visits_count' must be >= 1")

    # title (optional, but must be str if present)
    if "title" in item and item["title"] is not None and not isinstance(item["title"], str):
        fail(f"item {idx}: 'title' must be a string when present")


def validate_history_json(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        fail(f"Failed to read/parse JSON '{path}': {e}")

    if "metadata" not in data or not isinstance(data["metadata"], dict):
        fail("Top-level 'metadata' object missing or not an object")
    if "history" not in data or not isinstance(data["history"], list):
        fail("Top-level 'history' array missing or not an array")

    validate_metadata(data["metadata"])

    if len(data["history"]) == 0:
        fail("No items in 'history' array (Safari rejects empty history)")

    for i, item in enumerate(data["history"], 1):
        if not isinstance(item, dict):
            fail(f"item {i}: must be an object")
        validate_item(item, i)

    print(f"✅ Schema looks OK — {len(data['history'])} item(s).")
    print("   metadata:", data["metadata"])


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python validator.py /path/to/History.json")
        return 2
    validate_history_json(sys.argv[1])
    return 0


if __name__ == "__main__":
    main()
