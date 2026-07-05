"""
llm/transcripts.py — Transcript record/replay manager.

When REPLAY_LLM=true (the default graded path):
  - Every LLM call is intercepted and replaced with the pre-committed response
    from transcripts/index.json + transcripts/*.json
  - The key is "{agent}_{record_id}"
  - Transcript filename is the sha256 hex of the response JSON (canonical)

When REPLAY_LLM=false (real LLM path):
  - Real API calls are made
  - Every call is recorded and appended to transcripts/

Transcript file format:
{
  "agent": "Worker",
  "model": "gpt-4o-mini",
  "prompt_version": "worker-v1",
  "record_id": "REC-001",
  "request": { ... },
  "response": { ... },
  "response_hash": "sha256:<hex>",       # sha256(canon(response))
  "delivered_fields_hash": "sha256:<hex>", # sha256(canon(delivered_fields))
  "ts": "2026-06-26T00:00:00Z"
}

Filename: {response_hash_hex}.json  (the hex part after "sha256:")

index.json maps "{agent}_{record_id}" → "sha256:{hex}"
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from agents.contracts import canon, sha256_hex


TRANSCRIPTS_DIR = Path(os.getenv("TRANSCRIPTS_DIR", "transcripts"))


class TranscriptManager:
    """
    Manages reading pre-committed transcripts (REPLAY_LLM=true)
    and writing new ones (REPLAY_LLM=false).
    """

    def __init__(self, transcripts_dir: Path = TRANSCRIPTS_DIR):
        self.dir = transcripts_dir
        self._index: dict[str, str] = {}   # "{agent}_{record_id}" → "sha256:{hex}"
        self._cache: dict[str, dict] = {}  # sha256_hex → transcript dict
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        idx_path = self.dir / "index.json"
        if idx_path.exists():
            with open(idx_path, encoding="utf-8") as f:
                self._index = json.load(f)
        self._loaded = True

    def _load_transcript(self, tx_hash: str) -> Optional[dict]:
        """Load a transcript by its sha256 hash (format: 'sha256:hexhex')."""
        if tx_hash in self._cache:
            return self._cache[tx_hash]
        stem = tx_hash.split(":")[-1]
        path = self.dir / f"{stem}.json"
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
        self._cache[tx_hash] = t
        return t

    def lookup(self, agent: str, record_id: str) -> Optional[dict]:
        """
        Look up a committed transcript for (agent, record_id).
        Returns the full transcript dict, or None if not found.
        """
        self._ensure_loaded()
        key = f"{agent}_{record_id}"
        tx_hash = self._index.get(key)
        if not tx_hash:
            return None
        return self._load_transcript(tx_hash)

    def record(
        self,
        agent: str,
        record_id: str,
        model: str,
        prompt_version: str,
        request: dict,
        response: dict,
        delivered_fields: Optional[dict] = None,
    ) -> str:
        """
        Persist a new transcript (called when REPLAY_LLM=false).
        Returns the transcript hash ('sha256:hexhex').
        """
        self.dir.mkdir(parents=True, exist_ok=True)

        resp_hash = sha256_hex(response)
        stem = resp_hash.split(":")[-1]

        df_hash = sha256_hex(delivered_fields) if delivered_fields is not None else None

        transcript = {
            "agent": agent,
            "model": model,
            "prompt_version": prompt_version,
            "record_id": record_id,
            "request": request,
            "response": response,
            "response_hash": resp_hash,
            "delivered_fields_hash": df_hash,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        tx_path = self.dir / f"{stem}.json"
        if not tx_path.exists():
            with open(tx_path, "w", encoding="utf-8") as f:
                json.dump(transcript, f, indent=2, ensure_ascii=False)

        # Update index (load+update+rewrite atomically)
        self._ensure_loaded()
        key = f"{agent}_{record_id}"
        self._index[key] = resp_hash

        idx_path = self.dir / "index.json"
        tmp_path = self.dir / "index.tmp.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._index, f, indent=2, sort_keys=True, ensure_ascii=False)
        tmp_path.replace(idx_path)

        self._cache[resp_hash] = transcript
        return resp_hash

    def get_response_hash(self, agent: str, record_id: str) -> Optional[str]:
        """Get just the transcript hash for a given (agent, record_id)."""
        self._ensure_loaded()
        key = f"{agent}_{record_id}"
        return self._index.get(key)


# Singleton — shared across the pipeline
_manager: Optional[TranscriptManager] = None


def get_transcript_manager() -> TranscriptManager:
    global _manager
    if _manager is None:
        _manager = TranscriptManager()
    return _manager
