"""
llm/client.py — Unified LLM client supporting replay + real modes.

REPLAY_LLM=true  (default, offline):
    LLM calls are intercepted and replaced with the pre-committed transcript
    response. The pipeline runs deterministically without any API key.

REPLAY_LLM=false (real):
    Makes live API calls to gpt-4o-mini / claude-3-5-haiku / gemini-1.5-flash.
    Reads LLM_API_KEY, LLM_MODEL, LLM_BASE_URL from environment.
    Every call is recorded to transcripts/ for reproducibility.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from agents.contracts import sha256_hex
from llm.transcripts import TranscriptManager, get_transcript_manager

REPLAY_LLM = os.getenv("REPLAY_LLM", "true").lower() != "false"
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")  # optional custom endpoint


class LLMClient:
    """
    Unified client that dispatches to replay or real mode.
    One instance per agent (agent name is baked in for transcript keying).
    """

    def __init__(self, agent_name: str, tm: Optional[TranscriptManager] = None):
        self.agent_name = agent_name
        self.tm = tm or get_transcript_manager()

    def complete(
        self,
        messages: list[dict],
        record_id: str,
        model: str,
        prompt_version: str,
        response_schema: Optional[dict] = None,
        delivered_fields: Optional[dict] = None,
    ) -> tuple[dict, int, int, float, str]:
        """
        Make an LLM call (or replay it).

        Returns:
            (response_dict, tokens_in, tokens_out, latency_ms, transcript_hash)
        """
        if REPLAY_LLM:
            return self._replay(record_id, model, prompt_version, messages)
        else:
            return self._real_call(
                messages, record_id, model, prompt_version,
                response_schema, delivered_fields
            )

    # ── Replay path ──────────────────────────────────────────────────────────

    def _replay(
        self,
        record_id: str,
        model: str,
        prompt_version: str,
        messages: list[dict],
    ) -> tuple[dict, int, int, float, str]:
        tx = self.tm.lookup(self.agent_name, record_id)
        if tx is None:
            raise RuntimeError(
                f"[REPLAY] No transcript for agent={self.agent_name!r} "
                f"record_id={record_id!r}. "
                f"Run scripts/generate_transcripts.py or set REPLAY_LLM=false."
            )
        response = tx["response"]
        # Simulate token counts from the transcript (or estimate)
        tokens_in = tx.get("tokens_in", _estimate_tokens(messages))
        tokens_out = tx.get("tokens_out", _estimate_tokens([response]))
        tx_hash = tx.get("response_hash", sha256_hex(response))
        return response, tokens_in, tokens_out, 0.0, tx_hash

    # ── Real call path ───────────────────────────────────────────────────────

    def _real_call(
        self,
        messages: list[dict],
        record_id: str,
        model: str,
        prompt_version: str,
        response_schema: Optional[dict],
        delivered_fields: Optional[dict],
    ) -> tuple[dict, int, int, float, str]:
        t0 = time.time()
        response_dict, tokens_in, tokens_out = _dispatch(model, messages, response_schema)
        latency_ms = (time.time() - t0) * 1000

        tx_hash = self.tm.record(
            agent=self.agent_name,
            record_id=record_id,
            model=model,
            prompt_version=prompt_version,
            request={"messages": messages, "model": model},
            response=response_dict,
            delivered_fields=delivered_fields,
        )
        return response_dict, tokens_in, tokens_out, latency_ms, tx_hash


# ── Dispatcher: routes to the right SDK ─────────────────────────────────────

def _dispatch(
    model: str,
    messages: list[dict],
    response_schema: Optional[dict],
) -> tuple[dict, int, int]:
    """Call the appropriate LLM SDK and return (parsed_response, tokens_in, tokens_out)."""
    if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
        return _call_openai(model, messages, response_schema)
    elif model.startswith("claude-"):
        return _call_anthropic(model, messages, response_schema)
    elif model.startswith("gemini-"):
        return _call_gemini(model, messages, response_schema)
    else:
        # Try OpenAI-compatible endpoint (LLM_BASE_URL set)
        return _call_openai(model, messages, response_schema)


def _call_openai(model: str, messages: list[dict], schema: Optional[dict]) -> tuple[dict, int, int]:
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai package not installed. Run: pip install openai")

    client = openai.OpenAI(
        api_key=LLM_API_KEY or None,
        base_url=LLM_BASE_URL or None,
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
    }
    if schema:
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or "{}"
    tokens_in = resp.usage.prompt_tokens if resp.usage else 0
    tokens_out = resp.usage.completion_tokens if resp.usage else 0
    return json.loads(content), tokens_in, tokens_out


def _call_anthropic(model: str, messages: list[dict], schema: Optional[dict]) -> tuple[dict, int, int]:
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    # Separate system prompt from messages
    system_msg = ""
    user_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            user_messages.append(m)

    client = anthropic.Anthropic(api_key=LLM_API_KEY)
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_msg + "\n\nIMPORTANT: Respond ONLY with valid JSON.",
        messages=user_messages,
    )
    content = resp.content[0].text
    tokens_in = resp.usage.input_tokens
    tokens_out = resp.usage.output_tokens
    return json.loads(content), tokens_in, tokens_out


def _call_gemini(model: str, messages: list[dict], schema: Optional[dict]) -> tuple[dict, int, int]:
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("google-generativeai package not installed.")

    genai.configure(api_key=LLM_API_KEY)
    gmodel = genai.GenerativeModel(model)
    # Collapse all messages into a single prompt
    prompt = "\n".join(
        f"[{m['role'].upper()}]: {m['content']}" for m in messages
    ) + "\n\nIMPORTANT: Respond ONLY with valid JSON."
    resp = gmodel.generate_content(prompt)
    content = resp.text
    # Gemini doesn't always expose token counts
    tokens_in = len(prompt.split()) * 4 // 3
    tokens_out = len(content.split()) * 4 // 3
    return json.loads(content), tokens_in, tokens_out


# ── Utility ──────────────────────────────────────────────────────────────────

def _estimate_tokens(objects: list) -> int:
    """Very rough token count estimate (4 chars ≈ 1 token)."""
    total_chars = sum(len(json.dumps(o, ensure_ascii=False)) for o in objects)
    return max(1, total_chars // 4)
