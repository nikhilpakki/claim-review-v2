"""Fixed list of procedure codes a rule can be scoped to. Kept in a JSON
data file rather than hardcoded so the list can grow without a code change.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "data", "procedure_codes.json")

_codes = None


def list_procedure_codes():
    global _codes
    if _codes is None:
        with open(_PATH, "r", encoding="utf-8") as f:
            _codes = json.load(f)
    return _codes
