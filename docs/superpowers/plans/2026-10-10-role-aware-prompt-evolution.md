# Role-Aware Prompt Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve and replay one backward-compatible prompt policy bundle whose shared constraints and role-specific policies are routed only to the appropriate EvoAgent roles.

**Architecture:** Add a pure policy-normalization and rendering module, use it from the existing `AgenticReviewer` prompt assembly, and extend the current evolution candidate rather than introducing a new API or database model. Production evolution compares baseline and candidate with an `AgentPolicyReplayReviewer` that reuses the complete Lead/Worker/Critic graph, while legacy callers retain the existing single-reviewer fallback.

**Tech Stack:** Python 3.11, standard library `json`/`hashlib`, existing EvoAgent runtime and `unittest` suite.

**Spec:** `docs/superpowers/specs/2026-10-10-role-aware-prompt-evolution-design.md`

## Global Constraints

- Keep every existing `/v1/evolution/*` request and response path compatible.
- Do not add or migrate database tables.
- Do not modify the four built-in role prompt constants.
- Treat prompt text, examples, feedback, repository content, and generated policy as untrusted data.
- A policy bundle uses one existing `skill_versions` version and activates or rolls back atomically.
- Old persisted candidates load permissively; strict shape and size validation applies only to new candidates.
- Full-agent replay failure blocks activation and must never silently fall back after the full replay factory is configured.
- Holdout case details remain redacted.

## Review Focus

- A generated candidate that changes only `role_policies` must not be mistaken for an unchanged prompt; Task 4 adds the identity test.
- Legacy structured candidates with malformed budgets must load with the default runtime budget rather than prevent startup; Task 2 adds the fallback test.
- Role text that happens to contain another role's marker must not defeat routing or de-duplication; Task 1 adds exact-section rendering tests.
- A configured full-agent replay that throws must produce evaluation errors rather than invoke the legacy reviewer; Task 4 adds the no-silent-fallback test.
- Activating or rolling back an old prompt version with no structured evolution run must produce an empty policy and remain usable; Task 5 adds the service reload test.

---

### Task 1: Policy schema, normalization, safety, and rendering

**Files:**
- Create: `evoagent/evolution_policy.py`
- Create: `tests/test_prompt_policy_evolution.py`

**Interfaces:**
- Produces: `KNOWN_ROLES: tuple[str, ...]`.
- Produces: `normalize_policy_candidate(candidate: object, strict: bool = True) -> dict`.
- Produces: `canonical_policy_json(candidate: dict) -> str`.
- Produces: `policy_sha256(candidate: dict) -> str`.
- Produces: `render_policy_for_role(candidate: dict, role: str, global_prompt: str = "") -> str`.
- Produces: `policy_budget(candidate: dict, role: str, default: int) -> int`.
- Depends only on the Python standard library so generator, runtime, and evolution engine can share it without import cycles.

- [ ] **Step 1: Write failing normalization and compatibility tests**

Add tests asserting that a legacy candidate with the five existing top-level fields normalizes without mutation, absent `role_policies` become empty role entries, canonical JSON/hash are stable across key order, and `strict=False` tolerates an invalid legacy budget while `policy_budget()` returns the supplied default.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m unittest tests.test_prompt_policy_evolution.PromptPolicyTests -v`

Expected: FAIL because `evoagent.evolution_policy` does not exist.

- [ ] **Step 3: Implement the schema constants and canonical normalizer**

Implement the exact role/field allowlists from the spec. Strict normalization must deep-copy supported data, add empty roles, reject unknown keys and types, enforce 20 prompt additions, 2,000 characters per addition, 10 examples, 4,000 characters per example side, 20 policy rules, known integer budget keys, and a 64 KiB canonical JSON limit. Permissive loading must preserve supported legacy content while dropping unusable role fragments rather than raising.

- [ ] **Step 4: Write failing safety and boundary tests**

Add table-driven assertions for unknown roles, role-inappropriate fields, boolean/non-integer budgets, every count/length boundary, dangerous instructions already forbidden by `EvolutionEngine`, source-edit requests, and a candidate just over 64 KiB.

- [ ] **Step 5: Implement deterministic safety validation**

Move the shared forbidden-token definition to `evolution_policy.py` so both prompt and role-policy checks use one source. Reject unsafe new prompt additions and few-shot text, including requests to emit or modify production `.py` source. Keep the existing public `EvolutionEngine.safety_evaluate()` response shape by importing the shared check later in Task 4.

- [ ] **Step 6: Write failing role-rendering and budget tests**

Assert that shared additions/examples render for every role, Lead alone receives legacy/new delegation rules, new tool policy renders only for its role, legacy top-level tool policy remains shared, another role's marker never appears, a top-level addition already present verbatim in `global_prompt` is not duplicated, and no `budget_parameters` key/value is rendered.

- [ ] **Step 7: Implement role rendering and runtime budget lookup**

Render deterministic titled sections with JSON-encoded examples/policies. Validate the requested role. `policy_budget()` must defensively parse permissive legacy data and clamp to `max(256, min(value, default * 4))`; missing or invalid values return `default`.

- [ ] **Step 8: Run policy tests**

Run: `python -m unittest tests.test_prompt_policy_evolution.PromptPolicyTests -v`

Expected: PASS.

- [ ] **Step 9: Commit the policy module**

```bash
git add evoagent/evolution_policy.py tests/test_prompt_policy_evolution.py
git commit -m "feat: add role-aware evolution policy schema"
```

### Task 2: Route policy to the correct runtime role

**Files:**
- Modify: `evoagent/agentic_core.py:285-335,839-992`
- Modify: `tests/test_prompt_policy_evolution.py`

**Interfaces:**
- Consumes: Task 1 normalization, rendering, and budget interfaces.
- Produces: `AgenticReviewer._role_prompt(role: str, selected_skills: Iterable[object] = ()) -> str`.
- Preserves: `AgenticReviewer.__init__()` and `review_with_context()` call compatibility.

- [ ] **Step 1: Write failing direct prompt-assembly tests**

Instantiate `AgenticReviewer` with a policy containing unique shared, Lead, Security, Reliability, and Critic markers. Assert `_role_prompt()` includes only the fixed prompt, shared marker, target marker, allowed legacy sections, and Worker-selected Skill instructions appropriate to that role.

- [ ] **Step 2: Write failing runtime budget tests**

Assert `_token_budget()` applies a valid role value, clamps low/high values, and uses the default for malformed policy loaded through the compatibility path. Assert the rendered prompts do not contain the budget key or numeric marker.

- [ ] **Step 3: Run the focused runtime tests and verify they fail**

Run: `python -m unittest tests.test_prompt_policy_evolution.RuntimePromptRoutingTests -v`

Expected: FAIL because runtime still appends the whole structured JSON to every prompt and lacks `_role_prompt()`.

- [ ] **Step 4: Implement centralized role prompt assembly**

Normalize `structured_config` with `strict=False` in `AgenticReviewer.__init__()`. Stop appending the entire JSON object to `prompt_overlay`. Implement `_role_prompt()` using the unchanged fixed role constants, `render_policy_for_role()`, and selected Skill instructions only for Security and Correctness/Reliability.

- [ ] **Step 5: Use the centralized builder in all role launches**

Replace ad hoc concatenation in `_run_lead()`, `_run_critic()`, and `_run_pending_assignments()`. Preserve Reliability's existing shared finding schema and all task/user-context fields. Replace `_token_budget()` parsing with `policy_budget()`.

- [ ] **Step 6: Add an end-to-end prompt capture test**

Use a fake `JsonChatClient` that records the system prompt passed to each role while returning valid phase responses. Run one review and assert Lead, Security, Correctness/Reliability, and Critic each received only their expected markers; assert Critic never received a Worker Skill.

- [ ] **Step 7: Run runtime and existing collaboration tests**

Run: `python -m unittest tests.test_prompt_policy_evolution.RuntimePromptRoutingTests tests.test_lead_worker_collaboration tests.test_role_token_budget -v`

Expected: PASS.

- [ ] **Step 8: Commit runtime routing**

```bash
git add evoagent/agentic_core.py tests/test_prompt_policy_evolution.py
git commit -m "feat: route evolved policy by agent role"
```

### Task 3: Generate validated role-aware candidates

**Files:**
- Modify: `evoagent/evolution_v2.py:10-75`
- Modify: `tests/test_prompt_policy_evolution.py`

**Interfaces:**
- Consumes: `normalize_policy_candidate(..., strict=True)` from Task 1.
- Preserves: `RootCauseEvolutionGenerator.generate(failures: list[dict], base_prompt: str) -> dict`.
- Produces: normalized `result["candidate"]` with optional `role_policies` and unchanged surrounding generation metadata.

- [ ] **Step 1: Write failing generator contract tests**

Use a fake JSON client to return one shared addition plus role-specific Lead and Security policies. Assert the returned candidate is normalized, `candidate_prompt` still contains the shared addition, the role policy is preserved, and the change diff remains a prompt-string diff.

- [ ] **Step 2: Write failing generator rejection tests**

Assert unknown roles, wrong role fields, unsafe few-shot content, invalid budgets, and source-code edit instructions raise `ValueError` before a version can be created.

- [ ] **Step 3: Run generator tests and verify they fail**

Run: `python -m unittest tests.test_prompt_policy_evolution.RootCauseGeneratorTests -v`

Expected: FAIL because `role_policies` is not allowed or validated.

- [ ] **Step 4: Extend the root-cause prompt contract**

Require cluster role attribution and the schema from the spec. Tell the generator to keep shared invariants at the top level, delegation under Lead, tool guidance under the invoking role, and budgets only where failure evidence indicates pressure.

- [ ] **Step 5: Normalize generated candidates before rendering**

Replace the local allowed-field-only validation with Task 1 strict normalization. Build `candidate_prompt` from normalized shared `prompt_additions`; return the normalized candidate and preserve existing clusters, rationale, ledger summary, hashes/diff inputs, and sanitized failure evidence.

- [ ] **Step 6: Run generator and existing advanced tests**

Run: `python -m unittest tests.test_prompt_policy_evolution.RootCauseGeneratorTests tests.test_advanced -v`

Expected: PASS.

- [ ] **Step 7: Commit role-aware generation**

```bash
git add evoagent/evolution_v2.py tests/test_prompt_policy_evolution.py
git commit -m "feat: generate role-aware prompt policies"
```

### Task 4: Evaluate complete policy bundles through the agent graph

**Files:**
- Modify: `evoagent/evolution.py:62-636`
- Modify: `tests/test_prompt_policy_evolution.py`
- Modify: `tests/test_advanced.py:64-255`

**Interfaces:**
- Consumes: Task 1 normalization/hash interfaces and the Task 2 role-aware `AgenticReviewer`.
- Produces: `AgentPolicyReplayReviewer(policy_spec: dict, client, token_budget: int = 8000, time_budget_seconds: int = 60, scanners: Optional[list] = None, skill_provider=None)`.
- Extends: `EvolutionEngine.__init__(..., policy_reviewer_factory: Optional[Callable[[dict], object]] = None)`.
- Extends internally: `_propose(..., activation_policy: str = "auto", structured_candidate: Optional[dict] = None)` without changing public `propose(skill_name, prompt, regression_score=None)`.
- Generalizes: `RegressionEvaluator.run(spec: object, cases: list[dict]) -> dict` while preserving string callers.

- [ ] **Step 1: Write failing replay-adapter tests**

Adapt the existing fake multi-agent client pattern to prove `AgentPolicyReplayReviewer.review()` runs Lead delegation, Worker review, Critic review, finalization, and FindingGate. Assert `evaluation_execution()`, `evaluation_collaboration()`, and `evaluation_config()` expose the last replay without leaking internal mutable state.

- [ ] **Step 2: Implement `AgentPolicyReplayReviewer`**

Add a minimal replay task store like the existing Skill replay store. Construct `AgenticReviewer` with the policy spec's global prompt and structured config, all four roles, supplied scanners/Skill provider, and bounded token/time configuration. Reuse `review_with_context()` and generate unique replay task IDs.

- [ ] **Step 3: Write failing engine selection and identity tests**

Cover full-agent factory selection, legacy factory fallback only when no policy factory exists, configured full replay exceptions counted as errors with no legacy retry, baseline structured policy lookup, and `evaluation_mode` persistence. Add the Review Focus case where prompt text is unchanged but a role policy hash differs; it must create and evaluate a new version. Add the inverse identical prompt-and-policy no-op case.

- [ ] **Step 4: Generalize the evaluator and engine policy specs**

Allow `RegressionEvaluator` to pass an opaque spec to its factory. In `EvolutionEngine`, load the active version's structured candidate from its matching evolution run, normalize baseline/candidate policies, and use `{prompt, structured_config}` specs when `policy_reviewer_factory` is configured. Keep strings for legacy factories.

- [ ] **Step 5: Persist policy gates and reproducibility metadata in the original run write**

Add `role_policy_valid`, `role_policy_safe`, and `evaluation_mode` to gates/metrics. Add candidate and baseline policy SHA-256 values. Store `structured_candidate` as part of the initial `save_evolution_run()` call so service reload never depends on a later best-effort update.

- [ ] **Step 6: Pass generated role policy into `_propose()`**

Update `auto_propose()` so role-only candidates are not discarded when `candidate_prompt == base`. Preserve `shadow_ready` behavior and generation metadata. Resolve failure cases only on `activated`; explicitly test that `shadow_ready`, `deferred`, and `rejected` retain them.

- [ ] **Step 7: Reuse shared prompt safety without changing its public result**

Make `safety_evaluate()` consume Task 1's shared forbidden-token logic while returning the current `safety_passed`, `completeness`, and `missing_terms` keys. Keep existing plain-prompt rejection behavior.

- [ ] **Step 8: Run evolution tests**

Run: `python -m unittest tests.test_prompt_policy_evolution.PolicyReplayTests tests.test_prompt_policy_evolution.EvolutionEnginePolicyTests tests.test_advanced tests.test_evolution_proof -v`

Expected: PASS.

- [ ] **Step 9: Commit full-agent policy replay**

```bash
git add evoagent/evolution.py tests/test_prompt_policy_evolution.py tests/test_advanced.py
git commit -m "feat: replay evolved policies through agent graph"
```

### Task 5: Wire production service loading and backward-compatible reload

**Files:**
- Modify: `evoagent/service.py:99-183,191-215`
- Modify: `tests/test_prompt_policy_evolution.py`
- Modify: `tests/test_service.py`

**Interfaces:**
- Consumes: `AgentPolicyReplayReviewer`, normalized persisted candidates, and existing active Skill/scanner providers.
- Produces: `ReviewService._structured_prompt_policy(version: Optional[int]) -> dict`.
- Produces: `ReviewService._build_policy_replay_reviewer(policy_spec: dict) -> AgentPolicyReplayReviewer`.
- Preserves: service construction, `reload_skills()`, API behavior, and existing `_build_llm_reviewer()` callers.

- [ ] **Step 1: Write failing active-policy lookup tests**

Persist an active legacy prompt without an evolution run and assert an empty policy. Persist a matching structured run and assert only that version's candidate loads. Persist unrelated runs and assert they are ignored; an explicitly activated version must load its matching bundle regardless of its original replay decision. Cover activation and rollback to a legacy version followed by `reload_skills()`.

- [ ] **Step 2: Implement one service helper for structured policy lookup**

Extract the existing inline run search into `_structured_prompt_policy(version)`. Return permissively normalized empty policy when version/run/config is absent. Use the helper in `_build_agentic_reviewer()` so normal reviews receive the correct bundle after activation or rollback.

- [ ] **Step 3: Write failing production factory tests**

Assert `EvolutionEngine.policy_reviewer_factory` is configured only when a model exists and produces `AgentPolicyReplayReviewer` with the service's model client, token/time budgets, deterministic scanners, and active Agent Skill provider. Assert no-model service retains the existing deferred behavior.

- [ ] **Step 4: Wire the full-agent replay factory**

Construct `EvolutionEngine` with both the legacy reviewer factory for compatibility and `_build_policy_replay_reviewer` for production full-agent evaluation. Do not catch replay failures in the service factory. Reuse existing providers rather than copying Skill/scanner loading logic.

- [ ] **Step 5: Add API compatibility coverage**

Exercise the existing plain `POST /v1/evolution/propose` payload and activation/reload path. Assert response keys remain present, metrics identify the evaluation mode, and no new request field is required.

- [ ] **Step 6: Run service, API, and production tests**

Run: `python -m unittest tests.test_prompt_policy_evolution.ServicePolicyIntegrationTests tests.test_service tests.test_production_features -v`

Expected: PASS.

- [ ] **Step 7: Commit production wiring**

```bash
git add evoagent/service.py tests/test_prompt_policy_evolution.py tests/test_service.py
git commit -m "feat: load role-aware prompt policy in service"
```

### Task 6: Document compatibility and verify the complete repository

**Files:**
- Modify: `README.md:210-299`
- Modify if required by verified behavior: `mianshi.md:207-215`

**Interfaces:**
- Consumes: all prior tasks' finalized schema and runtime behavior.
- Produces: user-facing documentation for role policy generation, routing, replay, activation, and compatibility.

- [ ] **Step 1: Update the Prompt evolution documentation**

Document the top-level legacy fields, optional `role_policies`, exact role routing, programmatic budgets, full-agent replay, atomic activation, and the fact that `/auto` still requires an explicit call. State that existing routes and stored versions require no migration.

- [ ] **Step 2: Run formatting and static repository checks**

Run: `git diff --check`

Expected: no output and exit code 0.

- [ ] **Step 3: Run the complete test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 4: Inspect the final diff for scope and compatibility**

Run: `git diff --stat && git diff -- evoagent/evolution_policy.py evoagent/evolution_v2.py evoagent/evolution.py evoagent/agentic_core.py evoagent/service.py README.md`

Expected: only the designed prompt-evolution, runtime-routing, tests, and documentation changes; no database migration, API route rename, fixed prompt rewrite, Skill-evolution redesign, or rollout expansion.

- [ ] **Step 5: Commit documentation and final verification fixes**

```bash
git add README.md mianshi.md
git commit -m "docs: explain role-aware prompt evolution"
```

- [ ] **Step 6: Record final evidence**

Capture the complete test command, pass count, elapsed time, final `git status --short`, and commit list in the implementation handoff. Do not claim completion without this fresh output.
