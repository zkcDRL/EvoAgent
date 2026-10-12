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
_DIRECTIVE_WRAPPERS = frozenset((",", ":", "("))
# Consume each lexical span once, including path-like spans without a .py
# suffix. A failed filename match must not rescan a long run of slashes.
# Quotes retain scope metadata; their contents use the same token stream.
_INSTRUCTION_TOKENS = re.compile(
    r"(?P<code>```[\s\S]*?(?:```|$))|"
    r"(?P<escaped>\\.)|(?P<word>[\w/-]+(?:\.[\w/-]+)*)|"
    r"(?P<boundary>[.!?;])|(?P<symbol>[^\w\s])",
    re.IGNORECASE,
)


class _InstructionToken:
    __slots__ = ("raw", "text", "kind", "start", "end", "quote", "newline")

    def __init__(self, match, quote, newline):
        self.raw = match.group()
        self.text = self.raw.lower()
        self.kind = match.lastgroup
        if self.kind == "word":
            if self.text.endswith(".py"):
                self.kind = "source"
            elif "." in self.text:
                self.kind = "identifier"
        self.start, self.end = match.span()
        self.quote = quote
        self.newline = newline


def _instruction_tokens(text: str) -> list:
    tokens = []
    quote = None
    delimiter = None
    quote_number = 0
    previous_end = 0
    for match in _INSTRUCTION_TOKENS.finditer(text):
        value = match.group()
        if match.lastgroup == "symbol" and value in ("`", "\"", "'"):
            # An apostrophe inside an unquoted word is a contraction, whereas
            # a closing single quote remains a boundary even beside app.py.
            if (value == "'" and delimiter is None and match.start() > 0
                    and match.end() < len(text) and text[match.start() - 1].isalnum()
                    and text[match.end()].isalnum()):
                continue
            if delimiter is None:
                quote_number += 1
                quote, delimiter = quote_number, value
            elif value == delimiter:
                quote, delimiter = None, None
            continue
        tokens.append(_InstructionToken(match, quote, "\n" in text[previous_end:match.start()]))
        previous_end = match.end()
    return tokens


def _token_text(token) -> str:
    return token.text


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
        if (index == end or tokens[index].kind != "word" or tokens[index].quote is not None
                or (not tokens[index].raw.islower()
                    and not (head.raw.isupper() and tokens[index].raw.isupper()))):
            return None
        if _token_text(tokens[index]) == "review":
            index += 1
        elif _token_text(tokens[index]) != "analysis":
            return None
        if index == end or tokens[index].kind != "word" or tokens[index].quote is not None:
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
        token = match.text
        if token in ("(", "["):
            openings.append((index, ")" if token == "(" else "]"))
        elif token in (")", "]"):
            if openings and token == openings[-1][1]:
                start, _ = openings.pop()
                ends[start - 1] = index + 1
            else:
                openings.clear()
    return ends


def _identifier_tokens(tokens: list) -> list:
    """Index each identifier's first call/name-list member in bounded passes."""
    owners = [None] * len(tokens)
    call_ends = _call_ends(tokens)
    index = 0
    previous_end = None
    owner = None
    while index < len(tokens):
        end = call_ends.get(index) if tokens[index].kind in ("word", "identifier") else None
        if end is not None and (tokens[index].end == tokens[index + 1].start or end == index + 3):
            coordinated = (previous_end is not None
                           and ((index == previous_end + 1
                                 and tokens[previous_end].text in ("and", ","))
                                or (index == previous_end + 2
                                    and tokens[previous_end].text == ","
                                    and tokens[previous_end + 1].text == "and")))
            if not coordinated:
                owner = index
            # Call ranges are disjoint because the cursor advances to their end.
            for member in range(index, end):
                owners[member] = owner
            previous_end = end
            index = end
        else:
            index += 1
    name_delimiters = _EVIDENCE_LINKS | {"in", "at", "of", "with", "and", ","}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _token_text(token) not in _SOURCE_HEADS | {"named", "called"}:
            index += 1
            continue
        cursor = index + 1
        owner = cursor
        while cursor < len(tokens) and tokens[cursor].kind in ("word", "identifier", "source"):
            end = call_ends.get(cursor, cursor + 1)
            if end < len(tokens) and _token_text(tokens[end]) not in name_delimiters:
                break
            # Name-list ranges are also disjoint within this pass. Retain the
            # first member so a quoted successor inherits its evidence origin.
            for member in range(cursor, end):
                owners[member] = owner
            cursor = end
            if end == len(tokens) or _token_text(tokens[end]) not in ("and", ","):
                break
            cursor = end + 1
            if cursor < len(tokens) and _token_text(tokens[cursor]) == "and":
                cursor += 1
        index = max(index + 1, cursor)
    return owners


def _directive_lead(tokens: list, index: int, prefix_starts: list,
                    bridge_starts: list, bridge_directives: list):
    """Use indexed prefix/bridge runs instead of scanning backwards per verb."""
    lead = prefix_starts[index]
    if not lead:
        return lead, True
    previous = _token_text(tokens[lead - 1])
    if previous in _DIRECTIVE_LINKS | _DIRECTIVE_WRAPPERS:
        return bridge_starts[lead - 1], bridge_directives[lead - 1]
    if tokens[lead - 1].kind == "boundary" or tokens[lead].newline:
        return lead, True
    return None


def _source_reference(token) -> bool:
    return (token.kind in ("source", "code", "identifier")
            or _token_text(token) in _SOURCE_OBJECTS | _SOURCE_HEADS)


def _clause_requests_source_change(tokens: list) -> bool:
    """Index fixed passes, then inspect disjoint directive-object ranges."""
    if not tokens:
        return False
    identifier_owners = _identifier_tokens(tokens)
    prefix_starts = [0] * len(tokens)
    bridge_starts = [0] * len(tokens)
    bridge_directives = [False] * len(tokens)
    quote_starts = {}
    directives = []
    bridge_continues = False
    for index, token in enumerate(tokens):
        prefix_starts[index] = index
        if index and (_token_text(tokens[index - 1]) in _DIRECTIVE_PREFIXES
                      or (tokens[index - 1].kind == "word" and tokens[index - 1].text.endswith("ly"))):
            prefix_starts[index] = prefix_starts[index - 1]
        bridge_starts[index] = index
        bridge_directives[index] = token.text in _DIRECTIVE_LINKS
        bridge_member = (token.text in _DIRECTIVE_LINKS | _DIRECTIVE_WRAPPERS | _DIRECTIVE_PREFIXES
                         or (token.kind == "word" and token.text.endswith("ly")))
        if index and bridge_member and bridge_continues:
            bridge_starts[index] = bridge_starts[index - 1]
            bridge_directives[index] |= bridge_directives[index - 1]
        bridge_continues = bridge_member
        if token.quote is not None:
            quote_starts.setdefault(token.quote, index)
        # Calls/name syntax identifies unquoted code. A quoted operation at a
        # directive lead still needs the enclosing evidence grammar below.
        if ((identifier_owners[index] is not None and token.quote is None)
                or token.kind != "word" or token.text not in _SOURCE_OPERATIONS):
            continue
        if index and tokens[index - 1].text in _SOURCE_HEADS | {"named", "called"}:
            continue
        lead = _directive_lead(tokens, index, prefix_starts, bridge_starts, bridge_directives)
        if lead is not None:
            directives.append((index, lead[0]))
    source_after = [False] * (len(tokens) + 1)
    for index in range(len(tokens) - 1, -1, -1):
        source_after[index] = _source_reference(tokens[index]) or source_after[index + 1]
    in_evidence = False
    evidence_quotes = set()
    for position, (index, lead) in enumerate(directives):
        quote = tokens[index].quote
        if quote is not None and in_evidence:
            start = quote_starts[quote]
            owner = identifier_owners[index]
            origin_start = (start if owner is None
                            else quote_starts.get(tokens[owner].quote, owner))
            origin = _directive_lead(tokens, origin_start,
                                     prefix_starts, bridge_starts, bridge_directives)
            # The enclosing evidence grammar owns the entire literal. An
            # explicit bridge at its opening instead establishes a directive.
            data_label = (start and tokens[start - 1].text
                          in _EVIDENCE_LINKS | {"string", "literal", "evidence"})
            # A list's established evidence/name label remains authoritative
            # across newlines, even when the later member opens another quote.
            owner_label = (owner is not None and origin_start
                           and tokens[origin_start - 1].text
                           in _EVIDENCE_LINKS | _SOURCE_HEADS | {"named", "called", "evidence"})
            if data_label or owner_label or origin is None or not origin[1]:
                evidence_quotes.add(quote)
        if quote in evidence_quotes:
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
    """Tokenize once, then screen disjoint scopes in a fixed number of passes."""
    clause = []
    for token in _instruction_tokens(text):
        if token.kind == "boundary" and token.quote is None:
            if _clause_requests_source_change(clause):
                return True
            clause = []
        else:
            clause.append(token)
    return _clause_requests_source_change(clause)


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
