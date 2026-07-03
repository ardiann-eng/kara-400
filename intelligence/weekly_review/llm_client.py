"""
OpenAI-compatible LLM adapter for the weekly review.

The router can change its free model lineup, so this client discovers models
from `/models`, picks the first preferred model that exists, and falls back on
chat errors. Output is raw JSON, then validated by local guardrails:
- field whitelist (drop hallucinated fields)
- bounded delta downgrade (>50% change)
- min-sample confidence downgrade
- one JSON repair retry per model
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

_DEFAULT_BASE_URL = "https://router.bynara.id/v1"
_DEFAULT_MODELS = ("mimo-v2.5-pro",)


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _api_key() -> str:
    key = _env_first("KARA_REVIEW_API_KEY", "BYNARA_API_KEY", "OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "KARA_REVIEW_API_KEY not set. Set it to your Bynara/OpenAI-compatible router key."
        )
    return key


def _base_url() -> str:
    return _env_first("KARA_REVIEW_BASE_URL", "BYNARA_BASE_URL", "OPENAI_BASE_URL") or _DEFAULT_BASE_URL


def _preferred_models(explicit_model: Optional[str] = None) -> list[str]:
    configured = _env_first("KARA_REVIEW_MODELS", "BYNARA_MODELS")
    if configured:
        models = [m.strip() for m in configured.split(",") if m.strip()]
    else:
        primary = os.getenv("KARA_REVIEW_MODEL", "").strip() or getattr(config.WEEKLY_REVIEW, "model_id", "")
        fallback = (
            os.getenv("KARA_REVIEW_MODEL_FALLBACK", "").strip()
            or getattr(config.WEEKLY_REVIEW, "model_fallback", "")
        )
        models = [m for m in (primary, fallback, *_DEFAULT_MODELS) if m]

    if explicit_model:
        models.insert(0, explicit_model)

    deduped: list[str] = []
    for model in models:
        if model not in deduped:
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


def _call_chat(base_url: str, model: str, messages: list[dict[str, str]], use_json_mode: bool) -> dict:
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.15,
        "max_tokens": int(os.getenv("KARA_REVIEW_MAX_TOKENS", "4096")),
    }
    if use_json_mode:
        body["response_format"] = {"type": "json_object"}

    resp = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers=_headers(),
        json=body,
        timeout=int(os.getenv("KARA_REVIEW_TIMEOUT_SEC", "90")),
    )
    resp.raise_for_status()
    payload = resp.json()
    content = payload["choices"][0]["message"]["content"]
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
        log.debug("weekly review JSON mode unsupported for %s, retrying without it: %s", model, e)
        return _call_chat(base_url, model, messages, use_json_mode=False)


def _validate_and_repair(
    output: dict,
    schema: dict,
    snapshot: dict,
    min_samples: int,
) -> tuple[dict, list[str]]:
    """
    Enforce guardrails on LLM output. Returns (cleaned_output, warnings).
    - Drop suggestions with unknown field names.
    - Downgrade confidence when sample_size < min_samples.
    - Downgrade confidence + flag when |delta| > 50%.
    - Verify `current` matches snapshot (soft: flag if mismatch, don't drop).
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
                sug["risk_notes"] = (sug.get("risk_notes") or "") + f" [insufficient_sample: n={n}<{min_samples}]"
        except (TypeError, ValueError):
            pass

        cleaned_suggestions.append(sug)

    output["config_suggestions"] = cleaned_suggestions
    output.setdefault("insights", [])
    output.setdefault("flags", [])
    output.pop("_provider_meta", None)
    return output, warnings


def run_llm_review(
    evidence_pack: dict,
    config_snapshot: dict,
    schema: dict,
    min_samples: int = 30,
    model: Optional[str] = None,
) -> dict:
    """
    Execute the full LLM review flow with model discovery, fallback, and validation.
    """
    base_url = _base_url()
    user_msg = build_user_message(evidence_pack, config_snapshot, schema, min_samples)
    models = _resolve_models(base_url, explicit_model=model)
    if not models:
        raise RuntimeError("No weekly review models configured or discovered.")

    retries = 0
    errors: list[str] = []
    raw: Optional[dict] = None
    used_model = models[0]

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
