"""Usage aggregation and pricing helpers."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from transcript_events import latest_usage, normalize_usage


DEFAULT_PRICING_TABLE = Path(__file__).with_name("claude_pricing.json")
DEFAULT_INSTALLED_PRICING_TABLE = Path(sys.prefix) / "share" / "claude-tmux-control" / "claude_pricing.json"
_PRICING_TABLE_CACHE: dict | None = None


def aggregate_turn_usage(events: Sequence[dict]) -> dict:
    totals: dict[str, int | float] = {}
    seen_usage_ids: set[tuple[str, str]] = set()
    for event in events:
        if _is_result_event(event):
            continue
        raw_usage = _extract_usage(event)
        if not raw_usage:
            continue
        usage_id = _usage_event_identity(event)
        if usage_id is not None:
            if usage_id in seen_usage_ids:
                continue
            seen_usage_ids.add(usage_id)
        usage = normalize_usage(raw_usage)
        if not usage:
            continue
        for key, value in usage.items():
            totals[key] = totals.get(key, 0) + value
    if totals:
        return totals
    return normalize_usage(latest_usage(events)) or {}


def count_turn_usage_calls(events: Sequence[dict]) -> int:
    count = 0
    seen_usage_ids: set[tuple[str, str]] = set()
    for event in events:
        if _is_result_event(event):
            continue
        raw_usage = _extract_usage(event)
        if not raw_usage:
            continue
        if not normalize_usage(raw_usage):
            continue
        usage_id = _usage_event_identity(event)
        if usage_id is not None:
            if usage_id in seen_usage_ids:
                continue
            seen_usage_ids.add(usage_id)
        count += 1
    return count


def result_total_cost(events: Sequence[dict]) -> dict | None:
    for event in reversed(events):
        if not _is_result_event(event):
            continue
        total_cost_usd = _numeric_value(event, "total_cost_usd")
        if total_cost_usd is not None:
            return {
                "estimated": False,
                "currency": "USD",
                "source": "claude_result_total_cost_usd",
                "turn_usd": round(float(total_cost_usd), 8),
            }
    return None


def estimate_turn_cost(
    model: str | None,
    usage: Mapping[str, object] | None,
    pricing_table: Mapping[str, object] | None = None,
) -> dict:
    if not usage:
        return {"estimated": False, "reason": "usage_unavailable"}
    if not model:
        return {"estimated": False, "reason": "model_unavailable"}

    table = pricing_table or load_pricing_table()
    if not table:
        return {"estimated": False, "reason": "pricing_table_unavailable"}

    selection = select_pricing_model(model, table)
    if selection is None:
        return {"estimated": False, "reason": "pricing_model_not_found", "model": model}

    model_id, model_pricing, match_type = selection
    rates = model_pricing.get("rates_per_mtok")
    if not isinstance(rates, dict):
        return {"estimated": False, "reason": "pricing_rates_missing", "model": model}

    cache_write_ttl = str(table.get("default_cache_write_ttl") or "1h")
    cache_write_key = "cache_write_1h" if cache_write_ttl == "1h" else "cache_write_5m"
    used_rates = {
        "input": _float_value(rates, "input"),
        "cache_read": _float_value(rates, "cache_read"),
        "cache_write": _float_value(rates, cache_write_key),
        "output": _float_value(rates, "output"),
    }
    if any(value is None for value in used_rates.values()):
        return {"estimated": False, "reason": "pricing_rates_incomplete", "model": model_id}

    line_items = {
        "input_usd": _usd_line_item(usage, "input_tokens", used_rates["input"]),
        "cache_read_usd": _usd_line_item(usage, "cache_read_tokens", used_rates["cache_read"]),
        "cache_write_usd": _usd_line_item(usage, "cache_write_tokens", used_rates["cache_write"]),
        "output_usd": _usd_line_item(usage, "output_tokens", used_rates["output"]),
    }
    turn_usd = round(sum(line_items.values()), 8)
    return {
        "estimated": True,
        "currency": str(table.get("currency") or "USD"),
        "pricing_version": str(table.get("version") or ""),
        "pricing_source": str(table.get("source_url") or ""),
        "pricing_checked_at": str(table.get("checked_at") or ""),
        "model": model_id,
        "model_match": match_type,
        "cache_write_ttl": cache_write_ttl,
        "rates_per_mtok": used_rates,
        "line_items": line_items,
        "turn_usd": turn_usd,
    }


def add_session_cost_to_turn_cost(cost: Mapping[str, object], state: Mapping[str, object] | None) -> dict:
    enriched = dict(cost)
    turn_usd = _numeric_value(enriched, "turn_usd")
    if turn_usd is None:
        return enriched
    previous_total = 0.0
    if isinstance(state, Mapping):
        cost_totals = state.get("cost_totals")
        if isinstance(cost_totals, Mapping):
            previous_total = float(_numeric_value(cost_totals, "session_usd") or 0.0)
    enriched["session_usd"] = round(previous_total + float(turn_usd), 8)
    return enriched


def usage_totals_from_completed_turns(turns: Sequence[Mapping[str, object]]) -> dict:
    totals: dict[str, int | float] = {}
    for turn in turns:
        usage = turn.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
            value = _numeric_value(usage, key)
            if value is not None:
                totals[key] = totals.get(key, 0) + value
    return totals


def cost_totals_from_completed_turns(turns: Sequence[Mapping[str, object]]) -> dict:
    session_usd = 0.0
    has_cost = False
    for turn in turns:
        cost = turn.get("cost")
        if not isinstance(cost, Mapping):
            continue
        if cost.get("currency") != "USD":
            continue
        turn_usd = _numeric_value(cost, "turn_usd")
        if turn_usd is None:
            continue
        has_cost = True
        session_usd += float(turn_usd)
    if not has_cost:
        return {}
    return {"currency": "USD", "session_usd": round(session_usd, 8)}


def _turn_cost_for_completed_record(cost: object) -> dict | None:
    if not isinstance(cost, Mapping):
        return None
    turn_cost = dict(cost)
    turn_cost.pop("session_usd", None)
    return turn_cost


def resolve_pricing_table_path(path: Path = DEFAULT_PRICING_TABLE) -> Path:
    if path != DEFAULT_PRICING_TABLE:
        return path
    if path.exists():
        return path
    return DEFAULT_INSTALLED_PRICING_TABLE


def load_pricing_table(path: Path | None = None) -> dict | None:
    global _PRICING_TABLE_CACHE
    requested_path = path or DEFAULT_PRICING_TABLE
    uses_default_path = requested_path == DEFAULT_PRICING_TABLE
    if uses_default_path and _PRICING_TABLE_CACHE is not None:
        return _PRICING_TABLE_CACHE
    resolved_path = resolve_pricing_table_path(requested_path)
    try:
        table = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if uses_default_path:
        _PRICING_TABLE_CACHE = table
    return table


def select_pricing_model(model: str, table: Mapping[str, object]) -> tuple[str, Mapping[str, object], str] | None:
    models = table.get("models")
    if not isinstance(models, dict):
        return None

    normalized_model = _pricing_key(model)
    aliases: list[tuple[str, str]] = []
    for model_id, model_pricing in models.items():
        if not isinstance(model_id, str) or not isinstance(model_pricing, dict):
            continue
        values = [model_id]
        raw_aliases = model_pricing.get("aliases")
        if isinstance(raw_aliases, list):
            values.extend(alias for alias in raw_aliases if isinstance(alias, str))
        for alias in values:
            aliases.append((_pricing_key(alias), model_id))

    for alias_key, model_id in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if normalized_model == alias_key or normalized_model.startswith(alias_key + "-"):
            model_pricing = models.get(model_id)
            if isinstance(model_pricing, dict):
                return model_id, model_pricing, "exact"

    family = _pricing_family(normalized_model)
    families = table.get("families")
    if not family or not isinstance(families, dict):
        return None
    family_config = families.get(family)
    if not isinstance(family_config, dict):
        return None
    latest = family_config.get("latest")
    if not isinstance(latest, str):
        return None
    model_pricing = models.get(latest)
    if not isinstance(model_pricing, dict):
        return None
    return latest, model_pricing, "family_latest"


def _extract_usage(event: dict) -> dict:
    for candidate in (event.get("usage"), _nested_dict(event, "message", "usage"), _nested_dict(event, "response", "usage")):
        if isinstance(candidate, dict):
            return {str(key): value for key, value in candidate.items() if isinstance(value, int | float | str)}
    return {}


def _extract_context(event: dict) -> dict:
    for key in ("context", "context_window", "context_usage"):
        value = event.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items() if isinstance(v, int | float | str)}
    return {}


def _usage_event_identity(event: Mapping[str, object]) -> tuple[str, str] | None:
    request_id = event.get("requestId") or event.get("request_id")
    message_id = _nested_value(event, "message", "id") or _nested_value(event, "response", "id")
    if request_id is None and message_id is None:
        return None
    return (str(request_id or ""), str(message_id or ""))


def _is_result_event(event: Mapping[str, object]) -> bool:
    return str(event.get("type") or event.get("event") or "") == "result"


def _pricing_key(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def _pricing_family(normalized_model: str) -> str | None:
    if "sonnet" in normalized_model:
        return "sonnet"
    if "opus" in normalized_model:
        return "opus"
    if "haiku" in normalized_model or "hiku" in normalized_model:
        return "haiku"
    return None


def _usd_line_item(usage: Mapping[str, object], token_key: str, rate_per_mtok: float | None) -> float:
    tokens = _numeric_value(usage, token_key) or 0
    if rate_per_mtok is None:
        return 0.0
    return round(float(tokens) * rate_per_mtok / 1_000_000, 8)


def _float_value(source: Mapping[str, object], key: str) -> float | None:
    value = source.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _numeric_value(source: Mapping[str, object], *keys: str) -> int | float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int | float):
            return value
    return None


def _nested_value(source: dict, *keys: str) -> object:
    value: object = source
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _nested_dict(source: dict, *keys: str) -> dict | None:
    value: object = source
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, dict) else None
