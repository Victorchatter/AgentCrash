"""Secret/PII redaction for agent traces.

Runs at ingestion so secrets never reach storage in plaintext. Conservative by
default: err on redacting. No external dependencies — pattern + entropy based.

This is a defense-in-depth layer, not a guarantee. It catches the common cases
(API keys, bearer tokens, ``Authorization`` headers, env-var assignments, high
entropy blobs). Unknown secret shapes may still slip through; the safe-replay
execution modes are the hard boundary, not redaction alone.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Any

from agentcrash.schema import AgentCrashEvent, Privacy

# Ordered most-specific first. Each pattern -> a redaction type tag.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "anthropic_api_key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai_api_key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key_id"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "github_token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "slack_token"),
    (re.compile(r"(?i)authorization\s*[:=]\s*Bearer\s+[A-Za-z0-9_\-\.]+"), "bearer_token"),
    (re.compile(r"(?i)authorization\s*[:=]\s*[A-Za-z0-9_\-\.]{16,}"), "auth_header"),
    (re.compile(r"(?i)(?:api[_-]?key|secret|token|password|passwd|pwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}['\"]?"), "credential_assignment"),
    # env assignment: KEY=value where value looks secret-ish
    (re.compile(r"(?P<k>[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PWD|CREDENTIAL)[A-Z0-9_]*)\s*=\s*(?P<v>[^\s'\"]{8,})"), "env_secret"),
]


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Heuristic: a long-enough token with high entropy is probably a secret.
_ENTROPY_MIN_LEN = 32
_ENTROPY_THRESHOLD = 4.5
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")

# Dict keys whose *value* is a secret regardless of the value's shape. Redaction
# walks values independently of keys, so without this a header dict like
# ``{"Authorization": "Bearer <token>"}`` would leak — the bearer pattern needs
# the ``authorization:`` prefix inline, which is absent when the value is split
# out. Key-awareness closes that gap.
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization", "api_key", "apikey", "secret", "token", "password",
    "passwd", "pwd", "credential", "private_key", "access_key",
)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    kn = key.lower().replace("-", "_")
    # Count/usage keys contain a sensitive fragment ("token") but hold a
    # quantity, not the secret — substring matching would scrub them and lose
    # the counts (the OTel importer hit this with a "tokens" key). We use an
    # explicit allowlist of count/usage forms rather than word-bounding the
    # fragments, because word-bounding "token"/"secret"/"password" would also
    # stop redacting their plurals ("secrets", "passwords", "keys"), which ARE
    # secrets. Add a key here only when its value is provably not a secret.
    # ponytail: explicit allowlist; extend when a new count key surfaces.
    if kn.endswith(("_count", "_counts", "_usage", "_usages")):
        return False
    if kn in {"tokens", "token_usage"}:
        return False
    return any(frag in kn for frag in _SENSITIVE_KEY_FRAGMENTS)


def _redact_string(value: str) -> tuple[str, list[str]]:
    """Return (redacted_string, types_found)."""
    types: list[str] = []
    out = value
    for pat, tag in _PATTERNS:
        if pat.search(out):
            out = pat.sub(f"[REDACTED:{tag}]", out)
            types.append(tag)
    # Entropy sweep only on strings that look like bare tokens (not prose).
    # Skip if the string contains spaces (prose) unless it already matched.
    if " " not in value and "\n" not in value:
        for m in _TOKEN_RE.findall(value):
            if _shannon_entropy(m) >= _ENTROPY_THRESHOLD:
                out = out.replace(m, "[REDACTED:high_entropy]")
                types.append("high_entropy")
    return out, types


def _walk(value: Any) -> tuple[Any, list[str]]:
    types: list[str] = []
    if isinstance(value, str):
        red, t = _redact_string(value)
        return red, t
    if isinstance(value, dict):
        new: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                # The value sits under a secret-named key; redact it whole without
                # recursing (recursion could not improve on this and a nested
                # structure under "authorization" is still a secret).
                new[k] = "[REDACTED]"
                types.append("sensitive_key")
                continue
            rv, t = _walk(v)
            new[k] = rv
            types.extend(t)
        return new, types
    if isinstance(value, list):
        new_list: list[Any] = []
        for v in value:
            rv, t = _walk(v)
            new_list.append(rv)
            types.extend(t)
        return new_list, types
    if isinstance(value, tuple):
        new_tup = []
        for v in value:
            rv, t = _walk(v)
            new_tup.append(rv)
            types.extend(t)
        return tuple(new_tup), types
    return value, types


def redact_event(event: AgentCrashEvent) -> AgentCrashEvent:
    """Redact secrets in ``input``, ``output``, ``metadata`` in place + return it.

    Sets ``event.privacy`` accordingly. Errors and stack traces are also walked
    because exceptions frequently embed request bodies.
    """
    found: list[str] = []
    for attr in ("input", "output", "metadata"):
        val = getattr(event, attr)
        if val is None:
            continue
        red, t = _walk(val)
        setattr(event, attr, red)
        found.extend(t)
    # ReplayMeta.call_signature carries the raw call args (e.g. MCP tool
    # arguments) and is persisted alongside input/output — redact it too, or
    # secrets in args leak past the input/output scrub. ReplayMeta is frozen,
    # so rebuild it with the redacted signature.
    if event.replay is not None and event.replay.call_signature is not None:
        red_sig, t = _walk(event.replay.call_signature)
        if t:
            event.replay = event.replay.model_copy(update={"call_signature": red_sig})
            found.extend(t)
    if event.error is not None:
        for attr in ("message", "stack"):
            val = getattr(event.error, attr)
            if val:
                red, t = _redact_string(val)
                setattr(event.error, attr, red)
                found.extend(t)

    if found:
        # dedupe preserving order
        seen: set[str] = set()
        ordered = [x for x in found if not (x in seen or seen.add(x))]
        event.privacy = Privacy(redacted=True, redaction_types=ordered)
    return event


def redact_many(events: Iterable[AgentCrashEvent]) -> list[AgentCrashEvent]:
    return [redact_event(e) for e in events]