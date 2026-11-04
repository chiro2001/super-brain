from __future__ import annotations

import math
import re

PATTERN = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^\n]*\})?\s+([^\s]+)")
NAMES = {
    "vllm:num_requests_running": ("running", "sum"),
    "vllm:num_requests_waiting": ("waiting", "sum"),
    "vllm:kv_cache_usage_perc": ("kv_cache_usage", "max"),
    "vllm:gpu_cache_usage_perc": ("kv_cache_usage", "max"),
    "vllm:num_preemptions_total": ("preemptions_total", "sum"),
    "vllm:prompt_tokens_total": ("prompt_tokens_total", "sum"),
    "vllm:generation_tokens_total": ("generation_tokens_total", "sum"),
    "vllm:time_to_first_token_seconds_sum": ("ttft_sum", "sum"),
    "vllm:time_to_first_token_seconds_count": ("ttft_count", "sum"),
}


def parse_metrics(text: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for line in text.splitlines():
        if not (match := PATTERN.match(line)) or match[1] not in NAMES:
            continue
        try:
            value = float(match[2])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        key, aggregation = NAMES[match[1]]
        result[key] = max(result.get(key, value), value) if aggregation == "max" else result.get(key, 0) + value
    return result
