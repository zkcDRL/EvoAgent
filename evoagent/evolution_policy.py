"""Shared, standard-library-only evolution policy normalization and rendering."""

import copy
import hashlib
import json
import math
import re


KNOWN_ROLES = ("lead", "security", "correctness-reliability", "critic")
LEGACY_FIELDS = (
    "prompt_additions", "few_shot_examples", "lead_delegation_rules",
    "tool_selection_policy", "budget_parameters",
)
ROLE_FIELDS = {
    role: ("prompt_additions", "few_shot_examples",
           "delegation_rules" if role == "lead" else "tool_selection_policy")
    for role in KNOWN_ROLES
}
FORBIDDEN = ("ignore previous", "disable safety", "bypass", "直接执行生产")


def policy_text_is_safe(text: str) -> bool:
    """Deterministic instruction screening, shared with prompt safety checks."""
    lowered = text.lower()
    return not (any(token in lowered for token in FORBIDDEN)
                or re.search(r"\b(?:emit|modify|edit|rewrite|write|change|generate|produce|update|delete)\b"
                             r"[\s\S]{0,100}(?:\bsource(?:[- ]code)?\b|\bproduction\s+"
                             r"(?:python\s+)?code\b|\b[\w./-]+\.py\b|```python\b)", lowered)
                or re.search(r"(?:修改|编辑|生成|输出|重写)[^\n]{0,100}(?:源码|源代码)", lowered))


def _json_value(value: object) -> bool:
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    return False


def _safe_json_text(value: object) -> bool:
    if isinstance(value, str):
        return policy_text_is_safe(value)
    if isinstance(value, list):
        return all(_safe_json_text(item) for item in value)
    if isinstance(value, dict):
        return all(policy_text_is_safe(key) and _safe_json_text(item)
                   for key, item in value.items())
    return True


def _normalize_list(field: str, value: object, strict: bool) -> list:
    if not isinstance(value, list):
        if strict:
            raise ValueError(f"{field} must be an array")
        return []
    limit = 10 if field == "few_shot_examples" else 20
    if strict and len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} entries")
    result = []
    for item in value:
        if field == "prompt_additions":
            valid = isinstance(item, str)
            bounded = valid and len(item) <= 2000
        elif field == "few_shot_examples":
            valid = (isinstance(item, dict)
                     and (not strict or set(item) == {"input", "output"})
                     and all(isinstance(item.get(key), str) for key in ("input", "output"))
                     and _json_value(item))
            bounded = valid and all(len(item[key]) <= 4000 for key in ("input", "output"))
        else:
            valid = isinstance(item, dict) and _json_value(item)
            bounded = valid
        if strict and (not bounded or not _safe_json_text(item)):
            raise ValueError(f"{field} contains invalid, oversized, or unsafe content")
        if valid:
            result.append(copy.deepcopy(item))
    return result


def normalize_policy_candidate(candidate: object, strict: bool = True) -> dict:
    if not isinstance(candidate, dict):
        if strict:
            raise ValueError("candidate must be an object")
        candidate = {}
    if strict and set(candidate).difference((*LEGACY_FIELDS, "role_policies")):
        raise ValueError("candidate contains unsupported fields")
    normalized = {key: _normalize_list(key, candidate.get(key, []), strict)
                  for key in LEGACY_FIELDS if key != "budget_parameters"}
    budgets = candidate.get("budget_parameters", {})
    if not isinstance(budgets, dict):
        if strict:
            raise ValueError("budget_parameters must be an object")
        budgets = {}
    if strict and any(role not in KNOWN_ROLES or type(value) is not int
                      for role, value in budgets.items()):
        raise ValueError("budget_parameters requires known roles and integer values")
    normalized["budget_parameters"] = {
        role: value for role, value in budgets.items()
        if role in KNOWN_ROLES and type(value) is int
    }
    roles = candidate.get("role_policies", {})
    if not isinstance(roles, dict):
        if strict:
            raise ValueError("role_policies must be an object")
        roles = {}
    if strict and set(roles).difference(KNOWN_ROLES):
        raise ValueError("role_policies contains unknown roles")
    normalized["role_policies"] = {}
    for role in KNOWN_ROLES:
        policy = roles.get(role, {})
        if not isinstance(policy, dict):
            if strict:
                raise ValueError(f"{role} policy must be an object")
            policy = {}
        if strict and set(policy).difference(ROLE_FIELDS[role]):
            raise ValueError(f"{role} policy contains unsupported fields")
        normalized["role_policies"][role] = {
            field: _normalize_list(field, value, strict)
            for field, value in policy.items()
            if field in ROLE_FIELDS[role] and (strict or isinstance(value, list))
        }
    if strict and len(_serialize(normalized).encode("utf-8")) > 65536:
        raise ValueError("normalized policy exceeds 64 KiB")
    return normalized


def canonical_policy_json(candidate: dict) -> str:
    return _serialize(normalize_policy_candidate(candidate))


def _serialize(candidate: object) -> str:
    return json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def policy_sha256(candidate: dict) -> str:
    return hashlib.sha256(canonical_policy_json(candidate).encode("utf-8")).hexdigest()


def render_policy_for_role(candidate: dict, role: str, global_prompt: str = "") -> str:
    """Render a policy overlay; global_prompt is used only for deduplication.

    Loading is permissive because active and rollback versions may predate
    strict validation. Budgets are intentionally consumed only by the runtime.
    """
    if role not in KNOWN_ROLES:
        raise ValueError("unknown policy role")
    policy = normalize_policy_candidate(candidate, strict=False)
    sections = []

    def additions(title: str, values: list) -> None:
        if values:
            sections.append(title + ":\n- " + "\n- ".join(values))

    def structured(title: str, values: list) -> None:
        if values:
            sections.append(title + ":\n" + _serialize(values))

    additions("Shared evolution constraints", [item for item in policy["prompt_additions"]
                                                if item not in global_prompt])
    structured("Shared few-shot examples", policy["few_shot_examples"])
    if role == "lead":
        structured("Legacy Lead delegation rules", policy["lead_delegation_rules"])
    structured("Shared legacy tool selection policy", policy["tool_selection_policy"])
    role_policy = policy["role_policies"][role]
    additions(f"{role} evolution constraints", role_policy.get("prompt_additions", []))
    structured(f"{role} few-shot examples", role_policy.get("few_shot_examples", []))
    field = "delegation_rules" if role == "lead" else "tool_selection_policy"
    structured(f"{role} {field.replace('_', ' ')}", role_policy.get(field, []))
    return "\n\n".join(sections)


def policy_budget(candidate: dict, role: str, default: int) -> int:
    if role not in KNOWN_ROLES:
        return default
    budgets = candidate.get("budget_parameters", {}) if isinstance(candidate, dict) else {}
    value = budgets.get(role) if isinstance(budgets, dict) else None
    return max(256, min(value, default * 4)) if type(value) is int else default
