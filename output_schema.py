"""
output_schema.py — Loader for output_schema.yaml.
Exposes ALLOWED_FIELDS, REQUIRED_FIELDS, KNOWN_CATEGORIES and other schema definitions.
"""
from __future__ import annotations
import re
from pathlib import Path

def _load_simple_yaml(text: str) -> dict:
    data = {}
    current_section = None
    current_key = None
    
    lines = text.splitlines()
    for line in lines:
        line_strip = line.strip()
        if not line_strip or line_strip.startswith("#"):
            continue
            
        indent = len(line) - len(line.lstrip())
        
        m_li = re.match(r"^-\s*[\"']?(.*?)[\"']?$", line_strip)
        if m_li:
            val = m_li.group(1).strip()
            if " #" in val:
                val = val.split(" #")[0].strip()
            elif "#" in val:
                val = val.split("#")[0].strip()
                
            if current_key is not None:
                parent, key = current_key
                if isinstance(parent[key], list):
                    parent[key].append(val)
            continue
            
        m_kv = re.match(r"^([a-zA-Z0-9_\-]+)\s*:\s*(.*)$", line_strip)
        if m_kv:
            k = m_kv.group(1)
            v = m_kv.group(2).strip().strip('"').strip("'")
            if " #" in v:
                v = v.split(" #")[0].strip()
            elif "#" in v:
                v = v.split("#")[0].strip()
                
            if not v:
                if indent == 0:
                    data[k] = []
                    current_section = data[k]
                    current_key = (data, k)
                elif current_section is not None:
                    if isinstance(current_section, list):
                        for pk, pv in data.items():
                            if pv is current_section:
                                data[pk] = {}
                                current_section = data[pk]
                                break
                    if isinstance(current_section, dict):
                        current_section[k] = []
                        current_key = (current_section, k)
            else:
                if indent == 0:
                    data[k] = v
                elif current_section is not None:
                    if isinstance(current_section, list):
                        for pk, pv in data.items():
                            if pv is current_section:
                                data[pk] = {}
                                current_section = data[pk]
                                break
                    if isinstance(current_section, dict):
                        current_section[k] = v
            continue
            
    return data

_schema_path = Path(__file__).parent / "output_schema.yaml"
try:
    with open(_schema_path, encoding="utf-8") as f:
        _schema = _load_simple_yaml(f.read())
    ALLOWED_FIELDS: list[str] = _schema.get("delivered_fields", {}).get("allowed", [])
    REQUIRED_FIELDS: list[str] = _schema.get("delivered_fields", {}).get("required", [])
    KNOWN_CATEGORIES: list[str] = _schema.get("known_categories", [])
except Exception:
    ALLOWED_FIELDS = [
        "id", "attorney", "case_type", "normalized_claim_amount",
        "matter_classification", "priority_level", "recommended_strategy",
        "case_summary", "law_firm_brand", "pipeline_version", "generated_at",
    ]
    REQUIRED_FIELDS = [
        "id", "attorney", "case_type", "normalized_claim_amount",
        "matter_classification", "priority_level", "recommended_strategy",
        "case_summary",
    ]
    KNOWN_CATEGORIES = ["ONBOARDING", "RENEWAL", "REVIEW", "REPORT", "INTAKE"]
