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
_SOURCE_OPERATIONS = frozenset((
    "modify", "edit", "rewrite", "change", "update", "delete",
    "emit", "write", "generate", "produce",
))
_SOURCE_OBJECTS = frozenset(("source", "source-code", "code", "implementation", "file", "files"))
_SOURCE_HEADS = frozenset((
    "function", "functions", "method", "methods", "class", "classes",
    "module", "modules", "generation",
))
_REVIEW_OBJECTS = frozenset(("summary", "report", "finding", "findings", "analysis"))
_REVIEW_ADVERBS = frozenset((
    "clearly", "specifically", "briefly", "concisely", "directly", "explicitly", "precisely",
))
_REVIEW_FORMATS = frozenset(("json", "yaml", "markdown", "html", "xml", "csv"))
_REVIEW_MODIFIERS = _REVIEW_FORMATS | frozenset((
    "review", "concise", "detailed", "brief", "clear",
))
_EVIDENCE_LINKS = frozenset((
    "for", "based", "about", "describing", "on", "using", "regarding", "concerning", "from",
))
_DIRECTIVE_LINKS = frozenset((
    "and", "or", "but", "then", "to", "must", "should", "shall", "may", "can", "will",
    "would", "could",
))
_DIRECTIVE_PREFIXES = _REVIEW_ADVERBS | frozenset(("please", "always", "never", "not"))
# Keep quoted evidence and code atomic. In particular, an operation identifier
# inside a literal is not an imperative, and report.py is not a review noun.
_INSTRUCTION_TOKENS = re.compile(
    r"(?P<code>```[\s\S]*?(?:```|$))|"
    r"(?P<quoted>`[^`]*`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')|"
    r"(?P<source>[\w./-]+\.py\b)|(?P<identifier>\w+(?:\.\w+)+)|"
    r"(?P<word>\w+(?:[-']\w+)*)|(?P<boundary>[.!?;])|(?P<symbol>[^\w\s])",
    re.IGNORECASE,
)


def _token_text(token) -> str:
    value = token.group()
    return (value[1:-1] if token.lastgroup == "quoted" else value).lower()


def _review_artifact_end(tokens: list, start: int, end: int):
    """Parse one artifact, retaining identifier/quote boundaries for source heads."""
    index = start
    if index < end and _token_text(tokens[index]) in ("a", "an", "the"):
        index += 1
    while index < end and _token_text(tokens[index]) in _REVIEW_MODIFIERS:
        index += 1
    if index < end and _token_text(tokens[index]) in _SOURCE_HEADS:
        head = tokens[index]
        index += 1
        # Source heads modify only explicit artifact constructions. A quoted,
        # called, or capitalized identifier cannot borrow the artifact noun's
        # exemption (class `Summary`, function Analysis, function analysis()).
        if (index == end or tokens[index].lastgroup != "word"
                or (not tokens[index].group().islower()
                    and not (head.group().isupper() and tokens[index].group().isupper()))):
            return None
        if _token_text(tokens[index]) == "review":
            index += 1
        elif _token_text(tokens[index]) != "analysis":
            return None
        if index == end or tokens[index].lastgroup != "word":
            return None
    if index == end or _token_text(tokens[index]) not in _REVIEW_OBJECTS:
        return None
    while index < end and _token_text(tokens[index]) in _REVIEW_OBJECTS:
        index += 1
    while index < end:
        token = _token_text(tokens[index])
        if token in _REVIEW_ADVERBS:
            index += 1
        elif (token in ("in", "as") and index + 1 < end
              and _token_text(tokens[index + 1]) in _REVIEW_FORMATS):
            index += 2
            if index < end and _token_text(tokens[index]) == "format":
                index += 1
        else:
            break
    return index


def _review_object_end(tokens: list, start: int, end: int):
    """Recognize a complete artifact or coordinated artifact list."""
    index = _review_artifact_end(tokens, start, end)
    while index is not None and index < end and _token_text(tokens[index]) in ("and", ","):
        index += 1
        if index < end and _token_text(tokens[index]) == "and":
            index += 1
        index = _review_artifact_end(tokens, index, end)
    return index


def _evidence_link(tokens: list, index: int, end: int) -> bool:
    return (index < end and (_token_text(tokens[index]) in _EVIDENCE_LINKS
            or (_token_text(tokens[index]) == "with" and index + 1 < end
                and _token_text(tokens[index + 1]) == "evidence")))


def _call_ends(tokens: list) -> dict:
    """Index balanced calls/subscripts once, including nested expressions."""
    ends = {}
    openings = []
    for index, match in enumerate(tokens):
        token = match.group()
        if token in ("(", "["):
            openings.append((index, ")" if token == "(" else "]"))
        elif token in (")", "]"):
            if openings and token == openings[-1][1]:
                start, _ = openings.pop()
                ends[start - 1] = index + 1
            else:
                openings.clear()
    return ends


def _identifier_tokens(tokens: list) -> set:
    """Mark calls and explicit symbol-name lists before looking for directives."""
    identifiers = set()
    call_ends = _call_ends(tokens)
    index = 0
    while index < len(tokens):
        end = call_ends.get(index) if tokens[index].lastgroup in ("word", "identifier") else None
        if end is not None and (tokens[index].end() == tokens[index + 1].start() or end == index + 3):
            identifiers.update(range(index, end))
            index = end
        else:
            index += 1
    name_delimiters = _EVIDENCE_LINKS | {"in", "at", "of", "with", "and", ","}
    for index, token in enumerate(tokens):
        if _token_text(token) not in _SOURCE_HEADS | {"named", "called"}:
            continue
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor].lastgroup in ("word", "quoted", "identifier"):
            end = call_ends.get(cursor, cursor + 1)
            if end < len(tokens) and _token_text(tokens[end]) not in name_delimiters:
                break
            identifiers.update(range(cursor, end))
            if end == len(tokens) or _token_text(tokens[end]) not in ("and", ","):
                break
            cursor = end + 1
            if cursor < len(tokens) and _token_text(tokens[cursor]) == "and":
                cursor += 1
    return identifiers


def _directive_lead(tokens: list, index: int, text: str, identifiers: set):
    """Find a syntactic directive lead, not arbitrary operation words in evidence."""
    quoted = tokens[index].lastgroup == "quoted"
    if index in identifiers or (not quoted and (tokens[index].lastgroup != "word"
                                                or _token_text(tokens[index]) not in _SOURCE_OPERATIONS)):
        return None
    if quoted and _token_text(tokens[index]) not in _SOURCE_OPERATIONS:
        if not any(_token_text(part) in _SOURCE_OPERATIONS
                   for part in _INSTRUCTION_TOKENS.finditer(tokens[index].group()[1:-1])):
            return None
    if index and _token_text(tokens[index - 1]) in _SOURCE_HEADS | {"named", "called"}:
        return None
    lead = index
    while lead and (_token_text(tokens[lead - 1]) in _DIRECTIVE_PREFIXES
                    or (tokens[lead - 1].lastgroup == "word" and _token_text(tokens[lead - 1]).endswith("ly"))):
        lead -= 1
    if not lead:
        return lead
    previous = _token_text(tokens[lead - 1])
    if previous in _DIRECTIVE_LINKS or previous in (",", ":", "("):
        lead -= 1
        while lead and _token_text(tokens[lead - 1]) in (",", "and", "then"):
            lead -= 1
        return lead
    if "\n" in text[tokens[lead - 1].end():tokens[lead].start()]:
        return lead
    return None


def _source_reference(token) -> bool:
    return (token.lastgroup in ("source", "code", "identifier")
            or _token_text(token) in _SOURCE_OBJECTS | _SOURCE_HEADS
            or (token.lastgroup == "quoted"
                and re.search(r"[\w./-]+\.py\b", token.group(), re.I) is not None))


def _clause_requests_source_change(tokens: list, text: str) -> bool:
    if not tokens:
        return False
    identifiers = _identifier_tokens(tokens)
    directives = [(index, lead) for index in range(len(tokens))
                  if (lead := _directive_lead(tokens, index, text, identifiers)) is not None]
    source_after = [False] * (len(tokens) + 1)
    for index in range(len(tokens) - 1, -1, -1):
        source_after[index] = _source_reference(tokens[index]) or source_after[index + 1]
    in_evidence = False
    for position, (index, lead) in enumerate(directives):
        if tokens[index].lastgroup == "quoted":
            # A literal after an evidence label stays data. Quoting an actual
            # directive at the root or after a directive bridge does not hide it.
            if in_evidence and (tokens[lead].group() == ":"
                                or (index + 1 < len(tokens) and _token_text(tokens[index + 1]) in ("in", "at", "of"))):
                continue
            # Keep the following object in scope even if formatting closes the
            # quote before it, as in `Delete production `app.py`.`.
            quoted_directive = (tokens[index].group()[1:-1] + " "
                                + text[tokens[index].end():tokens[-1].end()])
            if _requests_source_change(quoted_directive):
                return True
            continue
        # A coordinated/subordinate operation owns its own object. Its evidence
        # cannot turn an outer source object into a review artifact.
        end = directives[position + 1][1] if position + 1 < len(directives) else len(tokens)
        object_end = _review_object_end(tokens, index + 1, end)
        if object_end is not None and (object_end == end or _evidence_link(tokens, object_end, end)):
            in_evidence = _evidence_link(tokens, object_end, end)
            continue
        if source_after[index + 1]:
            return True
        in_evidence = False
    return False


def _requests_source_change(text: str) -> bool:
    """Screen bounded directives and their objects; evidence identifiers are data."""
    clause = []
    for token in _INSTRUCTION_TOKENS.finditer(text):
        if token.lastgroup == "boundary":
            if _clause_requests_source_change(clause, text):
                return True
            clause = []
        else:
            clause.append(token)
    return _clause_requests_source_change(clause, text)


def policy_text_is_safe(text: str) -> bool:
    """Deterministic instruction screening, shared with prompt safety checks."""
    lowered = text.lower()
    return not (any(token in lowered for token in FORBIDDEN)
                or _requests_source_change(text)
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
