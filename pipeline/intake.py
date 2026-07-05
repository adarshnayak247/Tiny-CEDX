"""
pipeline/intake.py — Stage 1: Parse all source formats and persist records.

Reads from SEED_DIR:
  - feed.json      → structured records (format="feed")
  - inbox/*.eml    → email records (format="eml")
  - inbox/*.pdf    → PDF records (format="pdf")

Persists every record to SQLite (intake.db).
Handles SUPERSEDED_VERSION: same ID, higher version → mark old as superseded.
Handles SCHEMA_DRIFT: unknown field names → remap via field_map.yaml.
"""
from __future__ import annotations

import email
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from agents.contracts import RawRecord, sha256_bytes, sha256_hex

SEED_DIR = Path(os.getenv("SEED_DIR", "seed"))
DB_PATH = Path(os.getenv("DB_PATH", "out/intake.db"))

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

# Load field map
_field_map_path = Path(__file__).parent.parent / "field_map.yaml"
_FIELD_MAP: dict[str, str] = {}   # alias → canonical
try:
    with open(_field_map_path, encoding="utf-8") as f:
        _fm = _load_simple_yaml(f.read())
    for canonical, aliases in _fm.get("mappings", {}).items():
        for alias in aliases:
            _FIELD_MAP[alias.lower()] = canonical
except Exception:
    pass


class IntakeStage:
    """
    Parses all seed formats and persists records to SQLite.
    Returns a list of RawRecord objects for downstream processing.
    """

    def __init__(self, seed_dir: Path = SEED_DIR, db_path: Path = DB_PATH):
        self.seed_dir = seed_dir
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id              TEXT NOT NULL,
                version         INTEGER NOT NULL DEFAULT 1,
                source_format   TEXT NOT NULL,
                source_hash     TEXT NOT NULL,
                raw_fields      TEXT NOT NULL,
                ingested_at     TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (id, version)
            )
        """)
        con.commit()
        con.close()

    def run(self) -> tuple[list[RawRecord], list[dict]]:
        """
        Parse all sources, persist, resolve superseded versions.
        Returns (raw_records, superseded_events).
        """
        raw_records: list[RawRecord] = []

        # 1. Parse feed.json
        feed_path = self.seed_dir / "feed.json"
        if feed_path.exists():
            raw_records.extend(self._parse_feed(feed_path))

        # 2. Parse inbox/
        inbox_dir = self.seed_dir / "inbox"
        if inbox_dir.exists():
            for f in sorted(inbox_dir.iterdir()):
                if f.suffix.lower() == ".eml":
                    rec = self._parse_eml(f)
                    if rec:
                        raw_records.append(rec)
                elif f.suffix.lower() == ".pdf":
                    rec = self._parse_pdf(f)
                    if rec:
                        raw_records.append(rec)

        # 3. Persist + detect superseded
        superseded_events = self._persist_all(raw_records)

        return raw_records, superseded_events

    # ── Parsers ───────────────────────────────────────────────────────────────

    def _parse_feed(self, path: Path) -> list[RawRecord]:
        raw_bytes = path.read_bytes()
        src_hash = sha256_bytes(raw_bytes)
        records = json.loads(raw_bytes)
        result = []
        for rec in records:
            rec_lower = {k.lower(): v for k, v in rec.items()}
            rec_id = str(rec_lower.get("id", "UNKNOWN"))
            result.append(RawRecord(
                id=rec_id,
                source_format="feed",
                source_hash=sha256_hex(rec),
                raw_fields=rec_lower,
                version=int(rec_lower.get("version", 1)),
            ))
        return result

    def _parse_eml(self, path: Path) -> RawRecord | None:
        raw_bytes = path.read_bytes()
        src_hash = sha256_bytes(raw_bytes)
        try:
            msg = email.message_from_bytes(raw_bytes)
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

            fields = self._parse_key_value(body)
            rec_id = str(fields.get("id", path.stem.split("_")[0]))
            return RawRecord(
                id=rec_id,
                source_format="eml",
                source_hash=src_hash,
                raw_fields=fields,
                version=int(fields.get("version", 1)),
            )
        except Exception as e:
            print(f"[INTAKE] Failed to parse {path.name}: {e}")
            return None

    def _parse_pdf(self, path: Path) -> RawRecord | None:
        raw_bytes = path.read_bytes()
        src_hash = sha256_bytes(raw_bytes)
        text = ""
        try:
            import pypdf
            reader = pypdf.PdfReader(path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            # Fallback to pure-Python ASCII85 + FlateDecode stream parser
            try:
                text = _extract_text_from_pdf_pure(raw_bytes)
            except Exception as e:
                print(f"[INTAKE] Failed to parse PDF {path.name} with fallback: {e}")
                return None

        if not text:
            print(f"[INTAKE] Empty text extracted from {path.name}")
            return None

        fields = self._parse_key_value(text)
        rec_id = str(fields.get("id", path.stem.split("_")[0]))
        return RawRecord(
            id=rec_id,
            source_format="pdf",
            source_hash=src_hash,
            raw_fields=fields,
            version=int(fields.get("version", 1)),
        )


    def _parse_key_value(self, text: str) -> dict[str, Any]:
        """
        Parse 'Key: Value' lines from email/PDF body into a dict.
        Keys are lowercased. Handles multi-line values.
        """
        fields: dict[str, Any] = {}
        lines = text.strip().splitlines()
        current_key: str | None = None
        current_val: list[str] = []

        for line in lines:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_\s]*?)\s*:\s*(.*)$", line)
            if m:
                if current_key:
                    val = " ".join(current_val).strip()
                    fields[current_key] = _coerce(val)
                current_key = m.group(1).strip().lower()
                current_val = [m.group(2).strip()]
            elif current_key and line.strip():
                current_val.append(line.strip())

        if current_key:
            val = " ".join(current_val).strip()
            fields[current_key] = _coerce(val)

        return fields

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist_all(self, records: list[RawRecord]) -> list[dict]:
        """
        Persist all records. Detect and resolve SUPERSEDED_VERSION.
        Returns list of supersession events (for the exception queue).
        """
        superseded_events = []
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        con = sqlite3.connect(self.db_path)

        for rec in records:
            # Check if a record with same ID but different version exists
            existing = con.execute(
                "SELECT version, status FROM records WHERE id = ?", (rec.id,)
            ).fetchall()

            if existing:
                existing_versions = [row[0] for row in existing]
                max_existing_version = max(existing_versions)

                if rec.version > max_existing_version:
                    # New version supersedes old ones
                    con.execute(
                        "UPDATE records SET status='superseded' WHERE id = ? AND version < ?",
                        (rec.id, rec.version),
                    )
                    superseded_events.append({
                        "id": rec.id,
                        "superseded_versions": [v for v in existing_versions if v < rec.version],
                        "active_version": rec.version,
                    })
                elif rec.version < max_existing_version:
                    # This record is itself superseded (older version arriving late)
                    con.execute(
                        "INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,'superseded')",
                        (rec.id, rec.version, rec.source_format, rec.source_hash,
                         json.dumps(rec.raw_fields), now),
                    )
                    con.commit()
                    continue
                # If same version, just update (idempotency)

            con.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?,'pending')",
                (rec.id, rec.version, rec.source_format, rec.source_hash,
                 json.dumps(rec.raw_fields), now),
            )

        con.commit()
        con.close()
        return superseded_events

    def load_from_db(self) -> list[RawRecord]:
        """Load all non-superseded records from the database."""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT id, version, source_format, source_hash, raw_fields "
            "FROM records WHERE status = 'pending' ORDER BY id, version"
        ).fetchall()
        con.close()
        return [
            RawRecord(
                id=row[0], version=row[1], source_format=row[2],
                source_hash=row[3], raw_fields=json.loads(row[4]),
            )
            for row in rows
        ]

    def get_superseded_ids(self) -> list[tuple[str, int]]:
        """Return (id, version) pairs that are superseded."""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT id, version FROM records WHERE status = 'superseded'"
        ).fetchall()
        con.close()
        return rows


def normalize_raw_fields(raw_fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """
    Apply field_map aliases to raw_fields.
    Returns (normalized_fields, list_of_drifted_field_names).
    """
    normalized: dict[str, Any] = {}
    drifts: list[str] = []

    for key, val in raw_fields.items():
        canonical = _FIELD_MAP.get(key.lower())
        if canonical and canonical not in raw_fields:
            normalized[canonical] = val
            drifts.append(f"{key} → {canonical}")
        else:
            normalized[key.lower()] = val

    return normalized, drifts


def _coerce(val: str) -> Any:
    """Try to coerce a string value to int, float, or None."""
    if val.lower() in ("none", "null", "n/a", ""):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def _extract_text_from_pdf_pure(content: bytes) -> str:
    """Robust fallback PDF stream parser supporting ASCII85Decode + FlateDecode."""
    import base64
    import zlib
    
    extracted_text = []
    start = 0
    while True:
        idx = content.find(b"stream", start)
        if idx == -1:
            break
        end = content.find(b"endstream", idx)
        if end == -1:
            break
            
        stream_data = content[idx+7:end]
        if stream_data.endswith(b"\r\n"):
            stream_data = stream_data[:-2]
        elif stream_data.endswith(b"\n"):
            stream_data = stream_data[:-1]
            
        try:
            # ReportLab standard ascii85 encoding
            if not stream_data.startswith(b"<~"):
                data_to_decode = b"<~" + stream_data
            else:
                data_to_decode = stream_data
            decoded = base64.a85decode(data_to_decode, adobe=True)
            decompressed = zlib.decompress(decoded)
            text_content = decompressed.decode("utf-8", errors="ignore")
            extracted_text.extend(_extract_strings_pure(text_content))
        except Exception:
            # Fallback to direct raw decompression
            try:
                decompressed = zlib.decompress(stream_data)
                text_content = decompressed.decode("utf-8", errors="ignore")
                extracted_text.extend(_extract_strings_pure(text_content))
            except Exception:
                pass
        start = end + 9
        
    return "\n".join(extracted_text)


def _extract_strings_pure(text: str) -> list[str]:
    """Character scanner to parse balanced/escaped PDF strings."""
    results = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '(':
            i += 1
            depth = 1
            escaped = False
            val_chars = []
            while i < n:
                c = text[i]
                if escaped:
                    val_chars.append(c)
                    escaped = False
                elif c == '\\':
                    escaped = True
                elif c == '(':
                    depth += 1
                    val_chars.append(c)
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        break
                    val_chars.append(c)
                else:
                    val_chars.append(c)
                i += 1
            results.append("".join(val_chars))
        i += 1
    return results

