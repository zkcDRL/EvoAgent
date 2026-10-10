# Role-Aware Prompt Evolution Design

## Purpose

EvoAgent currently evolves one global prompt overlay and attaches the same
structured candidate to every agent. This design makes prompt evolution aware
of the Lead, Security, Correctness/Reliability, and Critic roles while keeping
the current HTTP API, database schema, version model, and legacy candidates
compatible.

The intended outcome is:

- common review invariants remain shared by every role;
- role-specific instructions, examples, and policies reach only their target
  role;
- token budgets are enforced by the runtime and are not copied into prompts;
- candidates are evaluated through the production Lead-to-Worker-to-Critic
  graph;
- one complete policy bundle is activated and rolled back atomically.

This work does not redesign Skill evolution, change the four built-in role
prompts, add database tables, rename API routes, or complete the existing
shadow/canary rollout framework.

## Compatibility Requirements

The following interfaces remain valid:

- `POST /v1/evolution/auto`;
- `POST /v1/evolution/propose` with a plain `prompt` string;
- existing activation and rollback routes;
- rows already stored in `skill_versions` and `evolution_runs`;
- candidates containing only the existing top-level fields.

`skill_versions.prompt` remains the canonical global prompt string. Structured
role policy remains stored in the corresponding evolution run under
`metrics.structured_candidate.candidate`; no schema migration is required.

Old persisted candidates are read permissively. New candidates are validated
strictly before being saved. This distinction ensures a newly introduced size
or shape rule cannot make a previously active version unloadable or
unrollbackable.

## Policy Bundle Schema

The existing candidate fields remain supported:

```json
{
  "prompt_additions": [],
  "few_shot_examples": [],
  "lead_delegation_rules": [],
  "tool_selection_policy": [],
  "budget_parameters": {}
}
```

New candidates may add `role_policies`:

```json
{
  "prompt_additions": ["Shared constraint"],
  "few_shot_examples": [],
  "lead_delegation_rules": [],
  "tool_selection_policy": [],
  "budget_parameters": {
    "security": 4000
  },
  "role_policies": {
    "lead": {
      "prompt_additions": [],
      "few_shot_examples": [],
      "delegation_rules": []
    },
    "security": {
      "prompt_additions": [],
      "few_shot_examples": [],
      "tool_selection_policy": []
    },
    "correctness-reliability": {
      "prompt_additions": [],
      "few_shot_examples": [],
      "tool_selection_policy": []
    },
    "critic": {
      "prompt_additions": [],
      "few_shot_examples": [],
      "tool_selection_policy": []
    }
  }
}
```

The only valid roles are `lead`, `security`,
`correctness-reliability`, and `critic`. Role-specific allowed fields are:

| Role | Allowed fields |
|---|---|
| Lead | `prompt_additions`, `few_shot_examples`, `delegation_rules` |
| Security | `prompt_additions`, `few_shot_examples`, `tool_selection_policy` |
| Correctness/Reliability | `prompt_additions`, `few_shot_examples`, `tool_selection_policy` |
| Critic | `prompt_additions`, `few_shot_examples`, `tool_selection_policy` |

The normalized policy representation is canonical JSON so it can be hashed,
compared, and recorded reproducibly. Normalization must not mutate caller
input.

### Legacy Mapping

- top-level `prompt_additions` are shared constraints;
- top-level `few_shot_examples` remain shared examples;
- top-level `lead_delegation_rules` are rendered only for Lead;
- top-level `tool_selection_policy` retains its old shared behavior;
- top-level `budget_parameters` are consumed programmatically by role and are
  not rendered into any system prompt;
- absent `role_policies` normalize to empty role-specific policy.

Newly generated candidates should prefer `role_policies`. Legacy fields exist
for compatibility rather than as the preferred authoring form.

## Validation and Safety

Create a focused policy module that owns strict candidate normalization,
permissive legacy loading, deterministic validation, canonical serialization,
hashing, and role-specific rendering.

Strict validation for new candidates enforces:

- no unknown top-level candidate fields;
- no unknown roles or role fields;
- list/object/string types exactly match the schema;
- no more than 20 prompt additions per role;
- no prompt addition longer than 2,000 characters;
- no more than 10 few-shot examples per role;
- no few-shot input or output longer than 4,000 characters;
- no more than 20 policy rules per role;
- normalized policy JSON no larger than 64 KiB;
- budget keys name known roles and budget values are integers;
- prompt additions and examples pass the existing dangerous-instruction check;
- role policy cannot request production source-code edits.

Runtime budget enforcement remains `max(256, min(value,
default_token_budget * 4))`. Invalid new values are rejected during candidate
validation; defensive runtime fallback to the default budget remains in place
for legacy data.

Generator output that violates the schema fails candidate generation and does
not create a version. An explicitly proposed prompt that fails existing prompt
safety checks continues to be saved as rejected according to current behavior.

## Candidate Generation

The root-cause generator receives the active global prompt and sanitized
failure cases as it does today. Its output contract is extended with
`role_policies` and instructs the model to:

1. cluster failures by shared root cause;
2. attribute each cluster to one or more responsible roles;
3. select the smallest relevant change type;
4. place shared invariants in top-level `prompt_additions`;
5. place role-specific instructions and examples under `role_policies`;
6. place delegation behavior only under the Lead policy;
7. place tool guidance under the role that invokes the tool;
8. use `budget_parameters` only when the evidence indicates budget pressure.

The LLM proposes a hypothesis; it does not approve the candidate. Strict
normalization and replay gates make the decision.

## Runtime Prompt Assembly

`AgenticReviewer` gains one prompt-building path used by all four roles. The
effective system prompt is assembled in this order:

1. immutable built-in role prompt;
2. active global prompt string and shared prompt additions;
3. shared legacy few-shot examples and legacy policies;
4. the current role's prompt additions and few-shot examples;
5. the current role's specialized policy;
6. selected Agent Skill instructions, for Security and
   Correctness/Reliability workers only.

Distribution rules are:

- Lead receives its role policy and both legacy and new delegation rules;
- Security receives only Security role policy and Skills selected for its
  assignment;
- Correctness/Reliability receives only its role policy and Skills selected for
  its assignment;
- Critic receives only Critic policy and never receives Worker Skills;
- role budgets never appear in prompt text;
- new tool policy is rendered only for its target role;
- legacy top-level tool policy remains shared for backward compatibility.

The existing `LEAD_PROMPT`, `SECURITY_PROMPT`, `RELIABILITY_PROMPT`, and
`CRITIC_PROMPT` constants remain unchanged. Correctness/Reliability continues
to reuse the existing finding output schema. Rolling back to a legacy version
therefore restores existing behavior without code branching on version
numbers.

## Full-Agent Replay

Introduce an `AgentPolicyReplayReviewer` adapter with the same `review()`
interface used by `RegressionEvaluator`. It accepts a policy spec containing:

```json
{
  "prompt": "global prompt string",
  "structured_config": {}
}
```

Internally it builds the existing `AgenticReviewer` and calls
`review_with_context()`. It must reuse the production collaboration graph
rather than duplicate it:

```text
Lead delegate
  -> Security and/or Correctness/Reliability
  -> Lead assessment and optional single revision
  -> Critic blind review
  -> Lead finalization
  -> FindingGate
  -> expected-finding matching
```

Baseline and candidate runs use the same model, fixed role prompts, active
Skill set, evaluation cases, and runtime limits. Their only intentional
difference is the policy spec.

`EvolutionEngine` accepts an optional `policy_reviewer_factory`. When present,
both baseline and candidate use full-agent replay. Existing callers that only
provide `reviewer_factory` retain the legacy single-reviewer path. Evaluation
runs record one of:

- `full-agent-policy-replay`;
- `legacy-single-reviewer`.

The baseline policy spec combines the active prompt with the structured
candidate stored on the active version's evolution run. If the active version
has no structured candidate, its structured configuration is empty. The
candidate policy spec combines the proposed prompt with the newly normalized
candidate configuration.

## Gates and Activation

Existing objective metrics and gates remain authoritative:

- every baseline and candidate replay completes without errors;
- validation score improves by at least `eval_min_improvement`;
- protected validation metrics do not regress beyond
  `eval_max_metric_regression`;
- protected holdout metrics do not regress;
- holdout case details remain redacted.

Add deterministic gates:

- `role_policy_valid`;
- `role_policy_safe`;
- `evaluation_mode` for auditability.

A role-specific change is never activated independently. Prompt plus
structured configuration form one policy bundle and retain one
`skill_versions` version number. Activation and rollback therefore remain
atomic.

Unresolved failure cases are marked resolved only when a result is
`activated`. They remain unresolved for `rejected`, `deferred`, and
`shadow_ready` results.

The current automatic root-cause path may continue to produce `shadow_ready`.
Completing shadow execution and automatic promotion is outside this design.

## API and Persistence

No route or table changes are required.

- `/v1/evolution/propose` still accepts a plain prompt and evaluates it with an
  empty new role policy against the active baseline policy;
- `/v1/evolution/auto` produces the extended candidate;
- `skill_versions` stores global prompt text and activation state;
- `evolution_runs.metrics.structured_candidate` stores normalized structured
  policy and generation evidence;
- reproducibility metadata adds the normalized policy SHA-256 and evaluation
  mode;
- activation and rollback endpoints continue reloading the reviewer.

## Error Handling

- malformed generated policy returns a validation error and creates no new
  version;
- a model or tool error during replay counts as an evaluation error and blocks
  activation;
- unavailable full-agent replay falls back only when no
  `policy_reviewer_factory` was configured, never silently after a configured
  full replay fails;
- a missing structured run for an old active version means an empty structured
  policy, not a service startup failure;
- invalid legacy budget data falls back to the configured default budget;
- memory enrichment and nonessential audit metadata must not turn a completed
  replay into a false success or hide a replay failure.

## Testing Strategy

### Policy unit tests

- normalize a legacy candidate without changing its meaning;
- reject unknown roles, unknown role fields, invalid types, oversized values,
  unsafe instructions, source-edit requests, and invalid budgets;
- prove normalization does not mutate its input;
- prove canonical JSON and hashes are stable.

### Prompt assembly tests

- shared prompt additions reach all four roles;
- Lead alone receives Lead examples and delegation rules;
- Security cannot see Reliability or Critic policy;
- Correctness/Reliability cannot see Security examples;
- Critic cannot see Worker Skills;
- Workers see only Skills selected for their assignments;
- budgets do not occur in prompt text and still affect runtime limits;
- legacy top-level fields keep their documented compatibility behavior.

### Evolution replay tests

- a Security-specific candidate that fixes a confirmed miss can activate;
- a locally better Worker that is rejected by Critic or Lead does not improve
  the end-to-end result and cannot activate;
- validation improvement with holdout regression is rejected;
- any role execution failure blocks activation;
- no configured model yields `deferred` as before;
- `shadow_ready` does not resolve failure cases;
- `activated` resolves the cases it used;
- legacy prompt versions load and roll back;
- the existing plain-prompt propose request remains valid;
- persisted metrics identify evaluation mode and policy hash without exposing
  holdout case details.

## File Responsibilities

- `evoagent/evolution_policy.py`: policy schema, normalization, validation,
  canonical serialization, hashing, and role rendering;
- `evoagent/evolution_v2.py`: role-aware generator contract and generated
  candidate validation;
- `evoagent/evolution.py`: policy specs, full/legacy evaluator selection,
  gates, reproducibility metadata, and failure-case resolution semantics;
- `evoagent/agentic_core.py`: role-specific prompt assembly and programmatic
  budget consumption;
- `evoagent/service.py`: full-agent replay factory and active baseline policy
  loading;
- `tests/test_prompt_policy_evolution.py`: new policy isolation, validation,
  and full-agent replay coverage;
- `tests/test_advanced.py`: legacy prompt-evolution compatibility coverage;
- `README.md`: public schema, behavior, and compatibility documentation.

## Acceptance Criteria

The change is complete when:

1. existing prompt evolution API tests pass unchanged;
2. old prompt versions load, activate, and roll back without migration;
3. new role policy is visible only to its designated role;
4. budgets are enforced without being rendered into prompts;
5. production prompt evolution evaluates baseline and candidate through the
   full multi-agent graph;
6. activation remains atomic at the existing version boundary;
7. validation and holdout gates prevent unsafe or regressing candidates;
8. no database schema or public route changes are introduced.
