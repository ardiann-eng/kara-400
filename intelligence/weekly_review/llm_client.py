"""
OpenAI-compatible LLM adapter for the weekly AI audit.

Default gateway: https://api.ojwgeoubcweojfb.shop/v1
Model: mimo/mimo-v2.5-pro

Env (first match wins for key/url):
  KARA_REVIEW_API_KEY | MIMO_API_KEY | OPENAI_API_KEY
  KARA_REVIEW_BASE_URL | MIMO_BASE_URL | OPENAI_BASE_URL
  KARA_REVIEW_MODEL / KARA_REVIEW_MODELS | MIMO_MODEL
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import requests

import config
from .prompt_builder import SYSTEM_PROMPT, REVIEW_TOOL_SCHEMA, build_user_message

log = logging.getLogger("kara.weekly_review.llm")

_DEFAULT_BASE_URL = "https://api.ojwgeoubcweojfb.shop/v1"
_DEFAULT_MODELS = ("mimo/mimo-v2.5-pro", "mimo-v2.5-pro")


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _api_key() -> str:
    key = _env_first(
        "KARA_REVIEW_API_KEY",
        "MIMO_API_KEY",
        "BYNARA_API_KEY",
        "OPENAI_API_KEY",
    )
    if not key:
        # Last resort: config module (may be empty)
        key = (getattr(config, "AI_API_KEY", None) or "").strip()
    if not key:
        raise RuntimeError(
            "No AI API key set. Set KARA_REVIEW_API_KEY or MIMO_API_KEY."
        )
    return key


def _base_url() -> str:
    return (
        _env_first(
            "KARA_REVIEW_BASE_URL",
            "MIMO_BASE_URL",
            "BYNARA_BASE_URL",
            "OPENAI_BASE_URL",
        )
        or getattr(config, "AI_BASE_URL", None)
        or _DEFAULT_BASE_URL
    )


def _preferred_models(explicit_model: Optional[str] = None) -> list[str]:
    configured = _env_first("KARA_REVIEW_MODELS", "BYNARA_MODELS")
    if configured:
        models = [m.strip() for m in configured.split(",") if m.strip()]
    else:
        primary = (
            os.getenv("KARA_REVIEW_MODEL", "").strip()
            or os.getenv("MIMO_MODEL", "").strip()
            or getattr(config.WEEKLY_REVIEW, "model_id", "")
            or getattr(config, "AI_MODEL", "")
        )
        fallback = (
            os.getenv("KARA_REVIEW_MODEL_FALLBACK", "").strip()
            or getattr(config.WEEKLY_REVIEW, "model_fallback", "")
        )
        models = [m for m in (primary, fallback, *_DEFAULT_MODELS) if m]

    if explicit_model:
        models.insert(0, explicit_model)

    deduped: list[str] = []
    for model in models:
        if model and model not in deduped:
            deduped.append(model)
    return deduped


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }


def _list_available_models(base_url: str) -> set[str]:
    try:
        resp = requests.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        return {str(item.get("id")) for item in payload.get("data", []) if item.get("id")}
    except Exception as e:
        log.warning("weekly review model discovery failed, using configured order: %s", e)
        return set()


def _resolve_models(base_url: str, explicit_model: Optional[str] = None) -> list[str]:
    preferred = _preferred_models(explicit_model)
    available = _list_available_models(base_url)
    if not available:
        return preferred
    matched = [m for m in preferred if m in available]
    # Also allow prefix matches (some routers list bare ids)
    if not matched:
        for p in preferred:
            for a in available:
                if p == a or p.endswith(a) or a.endswith(p.split("/")[-1]):
                    matched.append(p)
                    break
    return matched or preferred


def _extract_json(text: str) -> dict:
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("empty LLM response")

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.S | re.I)
    if fenced:
        stripped = fenced.group(1).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def _max_tokens() -> int:
    return int(
        os.getenv("KARA_REVIEW_MAX_TOKENS")
        or getattr(config.WEEKLY_REVIEW, "max_tokens", 8192)
    )


def _timeout() -> int:
    return int(
        os.getenv("KARA_REVIEW_TIMEOUT_SEC")
        or getattr(config.WEEKLY_REVIEW, "timeout_sec", 120)
    )


def _call_chat(base_url: str, model: str, messages: list[dict[str, str]], use_json_mode: bool) -> dict:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": _max_tokens(),
        "stream": False,
    }
    if use_json_mode:
        body["response_format"] = {"type": "json_object"}

    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=_headers(),
        json=body,
        timeout=_timeout(),
    )
    if resp.status_code >= 400:
        log.error(
            "LLM HTTP %s model=%s body_snip=%s",
            resp.status_code,
            model,
            (resp.text or "")[:400],
        )
    resp.raise_for_status()
    payload = resp.json()
    msg = payload["choices"][0]["message"]
    content = msg.get("content") or ""
    # Some gateway models (mimo-v2.5-pro) put text in reasoning_content first
    if not str(content).strip():
        content = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if not str(content).strip() and msg.get("tool_calls"):
        # tool-call style JSON args
        try:
            content = msg["tool_calls"][0]["function"]["arguments"]
        except Exception:
            pass
    if not str(content).strip():
        raise ValueError(
            f"empty LLM content (finish={payload['choices'][0].get('finish_reason')})"
        )
    output = _extract_json(content)
    output.setdefault("_provider_meta", {})
    output["_provider_meta"].update(
        {
            "model_returned": payload.get("model"),
            "usage": payload.get("usage", {}),
        }
    )
    return output


def _call_once(base_url: str, model: str, user_msg: str) -> dict:
    json_schema = REVIEW_TOOL_SCHEMA["input_schema"]
    messages = [
        {
            "role": "system",
            "content": (
                SYSTEM_PROMPT
                + "\n\nReturn ONLY one valid JSON object. No markdown, no prose outside JSON. "
                + "The JSON object must match this schema:\n"
                + json.dumps(json_schema, indent=2)
            ),
        },
        {"role": "user", "content": user_msg},
    ]

    try:
        return _call_chat(base_url, model, messages, use_json_mode=True)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status not in (400, 404, 422):
            raise
        log.debug("JSON mode unsupported for %s, retrying without it: %s", model, e)
        return _call_chat(base_url, model, messages, use_json_mode=False)


def _validate_and_repair(
    output: dict,
    schema: dict,
    snapshot: dict,
    min_samples: int,
) -> tuple[dict, list[str]]:
    """
    Enforce guardrails on LLM output. Returns (cleaned_output, warnings).
    """
    warnings: list[str] = []
    schema_keys = set(schema.keys())

    def _lookup_current(field: str):
        if "." in field:
            group, name = field.split(".", 1)
            return snapshot.get("groups", {}).get(group, {}).get(name, {}).get("value")
        return snapshot.get("top_level", {}).get(field, {}).get("value")

    cleaned_suggestions = []
    for sug in output.get("config_suggestions", []) or []:
        field = sug.get("field")
        if field not in schema_keys:
            warnings.append(f"dropped suggestion for unknown field: {field!r}")
            continue

        live_val = _lookup_current(field)
        if live_val is not None and sug.get("current") != live_val:
            sug["risk_notes"] = (sug.get("risk_notes") or "") + (
                f" [warn: current={sug.get('current')!r} does not match live={live_val!r}]"
            )
            warnings.append(f"drift on {field}: llm={sug.get('current')} live={live_val}")

        try:
            cur = float(sug.get("current"))
            new = float(sug.get("suggested"))
            if cur != 0 and abs(new - cur) / abs(cur) > 0.50:
                if sug.get("confidence") == "high":
                    sug["confidence"] = "medium"
                sug["risk_notes"] = (sug.get("risk_notes") or "") + " [flag: large_change_>50%]"
                warnings.append(f"large delta on {field}: {cur} -> {new}")
        except (TypeError, ValueError):
            pass

        try:
            n = int(sug.get("sample_size", 0) or 0)
            if n < min_samples:
                if sug.get("confidence") in ("medium", "high"):
                    sug["confidence"] = "low"
                sug["risk_notes"] = (sug.get("risk_notes") or "") + (
                    f" [insufficient_sample: n={n}<{min_samples}]"
                )
        except (TypeError, ValueError):
            pass

        cleaned_suggestions.append(sug)

    output["config_suggestions"] = cleaned_suggestions
    output.setdefault("insights", [])
    output.setdefault("flags", [])
    output.setdefault("executive_summary", "")
    output.pop("_provider_meta", None)
    return output, warnings


def run_llm_review(
    evidence_pack: dict,
    config_snapshot: dict,
    schema: dict,
    min_samples: int = 30,
    model: Optional[str] = None,
) -> dict:
    """Execute the full LLM audit flow with fallback and validation."""
    base_url = _base_url()
    user_msg = build_user_message(evidence_pack, config_snapshot, schema, min_samples)
    models = _resolve_models(base_url, explicit_model=model)
    if not models:
        raise RuntimeError("No weekly review models configured or discovered.")

    retries = 0
    errors: list[str] = []
    raw: Optional[dict] = None
    used_model = models[0]

    log.info("weekly_review LLM base_url=%s models=%s", base_url, models)

    for candidate in models:
        used_model = candidate
        for attempt in range(2):
            try:
                raw = _call_once(base_url, candidate, user_msg)
                break
            except Exception as e:
                retries += 1
                errors.append(f"{candidate}: {e}")
                log.warning(
                    "weekly review LLM call failed (model=%s attempt=%d): %s",
                    candidate,
                    attempt + 1,
                    e,
                )
        if raw is not None:
            break

    if raw is None:
        return {
            "executive_summary": "LLM audit failed — no model responded successfully.",
            "insights": [],
            "config_suggestions": [],
            "flags": [{
                "severity": "critical",
                "message": f"LLM call failed for all models: {errors[-1] if errors else 'unknown error'}",
            }],
            "meta": {
                "model": used_model,
                "warnings": errors,
                "retries": retries,
                "failed": True,
                "base_url": base_url,
            },
        }

    provider_meta = raw.get("_provider_meta", {})
    cleaned, warns = _validate_and_repair(raw, schema, config_snapshot, min_samples)
    cleaned["meta"] = {
        "model": used_model,
        "model_returned": provider_meta.get("model_returned"),
        "usage": provider_meta.get("usage", {}),
        "warnings": warns + errors,
        "retries": retries,
        "failed": False,
        "base_url": base_url,
    }
    return cleaned
