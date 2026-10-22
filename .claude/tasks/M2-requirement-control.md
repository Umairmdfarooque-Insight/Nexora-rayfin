# M2 — Requirement Control

**Exit deliverable:** approved requirement contract. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-201 is unchanged (`Done`). T-202 to T-204 were `Needs Fabric re-test`; their dispatcher wiring is confirmed against `CONTRACTS.md#fabric-operations` and `config/critical_terms.yaml`/`config/roles.yaml` now read from `Files/config/` of `nexora_control` in a job (`app/config_files.py`) — status `Awaiting manual test` pending a live Fabric run. T-205 (UI) was reworked, built in `rayfin-workload/` (Overview and Contract tabs), and is also `Awaiting manual test`.

---

### T-201 — BusinessContract schema

**Depends on:** T-101 · **Spec:** `BusinessContract.yaml` (§10), FR-003, §8.2 · **Fabric hosting:** library and CLI only; not affected by the rework (stays `Done`). Its model is used unchanged by every UDF and job.

#### Problem statement
The existing `RequirementSpec` holds lists of strings. It can't express a KPI's formula intent, grain, time logic, exclusions or currency, or track which questions block progress. The spec requires a structured, versioned `BusinessContract.yaml` that the business owner approves and every later stage builds on.

#### Functional requirements
1. Pydantic models define the BusinessContract, as a superset of `RequirementSpec`.
2. A contract can be saved to YAML and loaded back without change.
3. A JSON Schema is exported for LLM structured output (T-202) and for external validation.
4. Validation enforces the rules listed below, and a CLI command validates a file.

#### API contracts
Module `app/contracts/business_contract.py`, with `BusinessContract`, `Kpi`, `OpenQuestion`, `LockedFact`, `Feasibility`, `load(path) -> BusinessContract` and `save(contract, path) -> str`, which returns the hash.

`runs/<run_id>/BusinessContract.yaml`:
```yaml
schema_version: 1
run_id: 6f1c…
version: 1
status: draft                 # draft | approved
domain: Sales
business_objective: Track monthly net revenue for 2024
grain: order line
entities: [Sales, Product, Customer]
kpis:
  - name: Net Revenue
    formula_intent: SUM of order line totals, excluding cancelled orders
    grain: order line
    filters: ["Order year = 2024"]
    time_logic: calendar month
    exclusions: ["cancelled orders"]
    currency: USD
    expected_examples:
      - { description: "Total 2024", value: null }
    status: draft             # draft | approved | data_gap
    rationale: Defined explicitly in the prompt
dimensions: [Month, Product Category]
filters: ["Order year = 2024"]
currency: USD
exclusions: ["cancelled orders"]
security:
  - { description: "Sales reps see only their region", applies_to: [Sales] }
open_questions:
  - { id: Q1, text: "How is Revenue defined?", blocking: true, reason: "Business-critical term without definition", source: rule, status: open, answer: null }
locked_facts:
  - { id: F1, text: "Net Revenue excludes cancelled orders", source_question_id: Q1, locked_at: "…" }
assumptions: ["Fiscal year = calendar year"]
feasibility: []               # filled in by T-402
```

Generated files: `schemas/business_contract.schema.json`.

CLI:
- `python -m app.cli contract validate FILE` prints `VALID` (exit `0`), or `INVALID` and one `path: message` line per error (exit `1`).
- `python -m app.cli contract schema` prints the JSON Schema.

#### Constraints
- `extra="forbid"`: unknown fields are errors.
- Validation rules:
  - KPI names are unique, case-insensitive.
  - Question IDs and fact IDs are unique.
  - `status: approved` requires no open blocking question, and every KPI must have `formula_intent`, `grain` and `currency`.
- Saving is deterministic: keys in a fixed order, UTF-8, LF line endings.
- No LLM involvement in this task.
- Also enforced: a locked fact's `source_question_id` must name an existing question. `locked_at` stays text (ISO 8601) so it round-trips exactly. `Feasibility` has the shape T-402 documents (`kpi`, `status`, `needed`, `missing`, `options`); T-402 fills it.
- `python -m app.cli contract schema --output FILE` writes the schema as UTF-8 with LF. Use it instead of `>`, because PowerShell's `>` saves UTF-16.
- `samples/contract_valid.yaml` is written in the canonical form `save()` produces (block style), so the round trip in AC 4 gives no diff. `schemas/business_contract.schema.json` is committed; regenerate it whenever the models change.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| YAML syntax error | `INVALID`, with the line and column number. |
| Empty file | `INVALID: empty document`. |
| Unknown field, such as a typo like `kpi:` | `INVALID: kpi: extra field not permitted`. |
| Duplicate KPI name, for example "Net Revenue" and "net revenue" | `INVALID`, naming both entries. |
| `schema_version` newer than supported | `INVALID: unsupported schema_version 2`. |

#### Acceptance criteria
1. **Valid sample.** Create `samples/contract_valid.yaml` using the example above, with no open questions, and run `python -m app.cli contract validate samples/contract_valid.yaml`. **Expected:** `VALID`, exit `0`.
2. **Missing field.** Copy it and delete the KPI's `formula_intent`, then set `status: approved`. **Expected:** `INVALID` with `kpis.0.formula_intent` in the message.
3. **Blocking question prevents approval.** Add an open blocking question while keeping `status: approved`. **Expected:** `INVALID`, naming the question.
4. **Round trip.** Load and save the valid file with a small script, then diff it with the original. **Expected:** no difference.
5. **Schema export.** Run `python -m app.cli contract schema > schemas/business_contract.schema.json`. **Expected:** valid JSON containing `kpis`.

---

### T-202 — Requirement Compiler with ambiguity detection

**Depends on:** T-201, T-102 · **Spec:** FR-002, §8.2, §1.1 principle 2, NFR-008, NFR-009 · **Fabric hosting:** job (`POST /runs/{id}/compile`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - The compile graph runs inside the job. The node-path log line (AC-7) is in the job's notebook output, and in Eventhouse after T-704.
> - A missing or invalid `config/critical_terms.yaml` (read from `Files/config/`) fails the job with `500 CONFIG_ERROR` naming the file. There is no API startup any more.
> - Contract files go through the artifact store (T-101); the 60 s model timeout applies inside the job.

#### Problem statement
A plain-English prompt must become a draft BusinessContract. Today the LLM may silently assume what "Revenue" means. The spec forbids silent assumptions for business-critical terms, and requires the run to stop at clarification while a material ambiguity remains (FR-002).

#### Functional requirements
1. Compile a run's prompt into a draft BusinessContract with schema-constrained LLM output.
2. **Deterministic ambiguity rule:** a configured business-critical term that appears without a definition always produces a **blocking** question. The LLM can add questions, but can't remove these.
3. Locked facts from earlier answers are carried into each recompile unchanged (they come into use from T-203).
4. If any blocking question is open, the run moves to `NEEDS_CLARIFICATION`. Otherwise it stays, or returns to, `CREATED`.
5. Every compile writes a new contract version.

#### API contracts
- `app/compiler/requirement_compiler.py`: `compile_requirement(run: Run, previous: BusinessContract | None) -> BusinessContract`.
- Config `config/critical_terms.yaml`:
  ```yaml
  terms:
    Revenue: [revenue, sales amount, turnover]
    Margin: [margin, gross margin, profit margin]
    Retention: [retention, churn]
    Same-Store Sales: [same-store sales, like-for-like]
  definition_patterns: ["{term} =", "{term} is defined as", "{term} means", "define {term} as"]
  ```
- `POST /runs/{run_id}/compile` returns `200 {"run": RunDetail, "contract": BusinessContract, "blocking_questions": int}`.
- Files written: `runs/<id>/BusinessContract.yaml` (current) and `runs/<id>/contracts/BusinessContract.v<n>.yaml` (history).
- First stage graph ([CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs)):
  - `app/graphs/base.py`: `run_graph(...)`, as defined there.
  - `app/graphs/compile.py`: `build_compile_graph(llm=…)`. Nodes: `llm_compile → validate_schema → (retry llm_compile with the validation errors) → apply_critical_terms → apply_locked_facts`.
  - `compile_requirement` runs this graph. The `/compile` endpoint then writes the contract version and makes the transition.
- Add `langgraph` to `requirements.txt`, pinned to a major version, for example `langgraph>=1.0,<2`.

#### Constraints
- Use the Responses API with JSON Schema structured output (`text.format` type `json_schema`, strict), built from T-201's schema. Then validate with pydantic.
- Only the prompt, the subject area, the desired outputs, the constraints and the previous contract are sent to the LLM. No source data is sent (NFR-008).
- 60-second timeout. On invalid output, retry once with the validation errors included in the prompt.
- Compiling is allowed only in `CREATED` and `NEEDS_CLARIFICATION`.
- The deterministic rule runs **after** the LLM, so it can't be skipped. In the graph, `apply_critical_terms` and `apply_locked_facts` sit on every path to the end.
- The retry is the graph's loop. The `validate_schema → llm_compile` edge allows at most 2 LLM attempts in total.
- Implementation notes:
  - The LLM schema is T-201's schema pruned to the fields the LLM may fill (no `run_id`, `version`, `status`, `locked_facts`, `feasibility`, and no KPI `status` or question `id`, `source`, `status`, `answer`) and made strict (every property required, no extra properties). Code sets those fields: the draft `status`, the run id, the version (previous version + 1), question `source: llm`, and `domain` (the run's subject area, else `Sales`).
  - If both LLM attempts are invalid the graph ends after `validate_schema` with no contract, so the "ends in `apply_locked_facts`" rule applies to every path that produces a contract. The failure is then reported as `502 LLM_INVALID_OUTPUT`.
  - Rule question ids are stable: `RULE-<TERM>` (for example `RULE-REVENUE`) and `RULE-METRIC`; LLM questions are numbered `Q1`, `Q2`, … A rule question that was answered in the previous contract (or has a locked fact) is not raised again.
  - An LLM question that mentions a term a rule question already covers is dropped as a duplicate. Entities and dimensions get a capital first letter and are de-duplicated ignoring case, so wording differences between runs don't change the structure (NFR-009).
  - Compiling a run in `CREATED` that still has no blocking question adds no history entry; the state only changes when the outcome differs from the current state.
  - Contract files live in `runs/<id>/` for both run stores (only the manifest is in the Fabric SQL store). `GET /runs/{id}` now fills `open_questions` with the open questions of the current contract (objects with `id`, `text`, `blocking`, `reason`, `source`).
  - The API logs the node path at INFO (`stage=compile run_id=… node_path=llm_compile -> … -> apply_locked_facts`), and a bad `config/critical_terms.yaml` stops the API at startup with a message naming the file.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| The LLM output is invalid twice | `502 LLM_INVALID_OUTPUT`. The state is unchanged, and the raw output is saved to `runs/<id>/logs/compile-<ts>.json`. |
| LLM timeout | `504 UPSTREAM_TIMEOUT`. The state is unchanged. |
| The content filter rejects the prompt | `422 PROMPT_REJECTED`, with a plain message. |
| The prompt names no measurable KPI | A blocking question: "Which metric should be measured?" |
| The LLM omits a deterministic question | It's re-added, with `source: rule`. |
| Compile called in `REQUIREMENT_APPROVED` | `409 WRONG_STATE`. |
| `critical_terms.yaml` missing or invalid | `500 CONFIG_ERROR` at startup, naming the file. |

#### Acceptance criteria
1. **Ambiguous Revenue stops.** Create a run with the prompt "Show revenue by month for 2024", then call `POST /runs/{id}/compile`. **Expected:** state `NEEDS_CLARIFICATION`, and an open question with `blocking: true` and `source: rule` asking how Revenue is defined.
2. **Explicit definition passes.** Create a run with "Show Net Revenue by month for 2024. Net Revenue = SUM(LineTotal) excluding cancelled orders, in USD." and compile it. **Expected:** no blocking Revenue question, state `CREATED`, and the KPI `formula_intent` mentions LineTotal and the exclusion.
3. **Repeatable structure.** Compile the AC-2 prompt twice, on two new runs. **Expected:** the same KPI names, dimensions and set of questions. Wording may differ (NFR-009).
4. **Valid file.** Run `python -m app.cli contract validate runs/<id>/BusinessContract.yaml`. **Expected:** `VALID`.
5. **Versioning.** Compile the same run twice. **Expected:** `contracts/BusinessContract.v1.yaml` and `.v2.yaml` exist.
6. **Wrong state.** Use the CLI to move a run to `REQUIREMENT_APPROVED`, then compile it. **Expected:** `409 WRONG_STATE`.
7. **Graph boundary.** After AC-1, run `python -m app.cli runs show <id>`. **Expected:** compiling added exactly one history entry (`CREATED → NEEDS_CLARIFICATION`), and the log line for the compile lists the node path, ending in `apply_locked_facts`.

---

### T-203 — Clarification loop

**Depends on:** T-202 · **Spec:** FR-002, FR-003, §8.2 (locked facts) · **Fabric hosting:** job for `…/answer` (it recompiles); udf for `…/dismiss` and `PUT …/contract/kpis/{kpi_name}`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - AC-3's `/compile` is a job.

#### Problem statement
Once a question is asked, the user needs a way to answer it. The answer must then become a **locked fact** that recompiles can't overwrite, and the business owner must be able to edit KPI definitions directly (FR-003).

#### Functional requirements
1. Answering a question stores the answer, marks the question `answered`, adds a locked fact, and recompiles the contract.
2. Locked facts are applied after every recompile, so they win over LLM output.
3. Non-blocking questions can be dismissed with a reason.
4. KPI fields can be edited while the contract is a draft.
5. When no blocking question remains open, the run moves `NEEDS_CLARIFICATION → CREATED`.

#### API contracts
| Method and path | Request | Response |
|---|---|---|
| `POST /runs/{id}/questions/{qid}/answer` | `{"answer": str (1–2000 chars)}` | `200 {"run", "contract"}` |
| `POST /runs/{id}/questions/{qid}/dismiss` | `{"reason": str}` | `200 {"run", "contract"}` |
| `PUT /runs/{id}/contract/kpis/{kpi_name}` | Partial `Kpi`, for example `{"formula_intent": "...", "currency": "EUR"}`, plus `"expected_hash": str` | `200 {"contract"}` |

`kpi_name` is URL-encoded and matched case-insensitively.

#### Constraints
- Answers and edits are allowed only while `status: draft`, in states `CREATED` and `NEEDS_CLARIFICATION`.
- `expected_hash` provides optimistic concurrency. If it doesn't match the current contract hash, the request returns `409 STALE_VERSION`.
- Recompiling after an answer follows T-202's constraints, and re-runs the `compile` graph.
- The back-and-forth with the user is a run-layer loop (`NEEDS_CLARIFICATION ⇄ CREATED`), not a paused graph. Each answer is a new API call that starts a new graph invocation ([CONTRACTS.md#stage-graphs](CONTRACTS.md#stage-graphs), rule 4).

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Unknown `qid` or KPI | `404 NOT_FOUND`. |
| Empty answer | `422`. |
| Re-answering an answered question | Allowed while in draft. The locked fact is replaced, and the old answer is kept in contract history. |
| Dismissing a blocking question | `409`: "blocking questions must be answered". |
| The answer triggers a follow-up question | The new question is added. The state reflects whether any question is blocking. |
| The LLM output contradicts a locked fact | The locked fact is re-applied, and a warning is recorded in the compile log. |
| Edit after approval | `409 CONTRACT_LOCKED`. |

#### Acceptance criteria
1. **Answer closes the question.** On the run from T-202 AC-1, answer Q1 with "Net Revenue = SUM(LineTotal) excluding cancelled orders". **Expected:** Q1 `status: answered`, a locked fact with `source_question_id: Q1`, and the KPI's `formula_intent` updated.
2. **State advances.** `GET /runs/{id}`. **Expected:** state `CREATED`, and the next action is "Compile requirement" or the approval step.
3. **Locked fact survives.** Call `/compile` again. **Expected:** the locked fact is unchanged, and Q1 isn't asked again.
4. **Dismiss rules.** Try to dismiss a blocking question. **Expected:** `409`. Dismiss a non-blocking one with a reason. **Expected:** `200`.
5. **Concurrent edit.** Call `PUT …/kpis/Net Revenue` with an old `expected_hash`. **Expected:** `409 STALE_VERSION`.

---

### T-204 — Approval service and Gate G1

**Depends on:** T-203, T-105 · **Spec:** FR-018, §12 G1, Business rules 1–2, §14 (record approvers) · **Fabric hosting:** udf for `POST /runs/{id}/gates/G1/check`, `GET /runs/{id}/approvals` and `POST /runs/{id}/approvals` (ACCEPT, REJECT, EDIT, EXPLAIN); job for `REGENERATE`; `GET /me` is the UDF `me()`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - `config/roles.yaml` is read from `Files/config/roles.yaml` of `nexora_control` on every call. For AC-4, edit that file (or change `config/roles.yaml` and run `python -m app.tools.deploy_home`), and restore it afterwards.
> - A missing or invalid roles file is `500 CONFIG_ERROR` on the first call that needs it (no API startup check).

#### Problem statement
There's no formal approval. The spec requires the KPI meaning to be approved by the business owner, through a gate with automated checks, and every approval to be attributable and time-stamped. After approval, the contract must be locked.

#### Functional requirements
1. A reusable approval service supports **Accept, Edit, Reject, Explain and Regenerate** for any artifact (FR-018).
2. Gate G1 runs automated checks on the contract and saves a gate result.
3. **Accept** runs G1. If it passes and the caller holds the `business_owner` role, the contract becomes `approved` and locked, and the run moves to `REQUIREMENT_APPROVED`.
4. **Reject** needs a comment. The comment becomes a blocking question from the reviewer, and the run moves to `NEEDS_CLARIFICATION`.
5. **Explain** returns the rationale for each KPI and question.
6. **Regenerate** recompiles, using the comment as guidance and keeping the locked facts.
7. Every action is recorded as an approval record.

#### API contracts
- Approval record and gate result shapes: [CONTRACTS.md#approval-record](CONTRACTS.md#approval-record) and [#gate-result](CONTRACTS.md#gate-result).
- Roles: `config/roles.yaml` ([CONTRACTS.md#roles](CONTRACTS.md#roles)).

| Method and path | Request | Response |
|---|---|---|
| `POST /runs/{id}/gates/G1/check` | — | `200 GateResult` |
| `POST /runs/{id}/approvals` | `{"artifact": "BusinessContract", "action": "ACCEPT"\|"EDIT"\|"REJECT"\|"EXPLAIN"\|"REGENERATE", "comment"?: str, "edits"?: {...}, "expected_hash": str}` | `200 {"approval", "run", "gate_result"?, "explanation"?}` |
| `GET /runs/{id}/approvals` | — | `200 [ApprovalRecord]` |

G1 checks:

| ID | Check |
|---|---|
| G1.1 | The contract passes schema validation |
| G1.2 | No blocking question is open |
| G1.3 | Every KPI has `formula_intent`, `grain` and `currency` |
| G1.4 | No KPI has `status: data_gap` |
| G1.5 | Feasibility flags are present. Before T-402 this is a `warning` "not yet assessed", not a `fail`. |

#### Constraints
- `approvals.jsonl` is append-only.
- The contract hash is stored in the approval record. A locked contract can't change, except by returning it to requirement analysis (T-701).
- Only `business_owner` can Accept or Reject a contract. Any authenticated user can Explain.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Accept while G1 fails | `409 GATE_FAILED`, listing the failed checks. Nothing is locked. |
| Caller lacks the `business_owner` role | `403 FORBIDDEN`. |
| Accept on an already approved contract | `409 ALREADY_APPROVED`. |
| `expected_hash` doesn't match | `409 STALE_VERSION`. |
| Reject without a comment | `422`. |
| Edit after approval | `409 CONTRACT_LOCKED`. |

#### Acceptance criteria
1. **Gate blocks.** On a run with an open blocking question, call `POST /runs/{id}/approvals` with `ACCEPT`. **Expected:** `409 GATE_FAILED`, with check `G1.2` failing.
2. **Approve.** Resolve the question and Accept as a `business_owner`. **Expected:** state `REQUIREMENT_APPROVED`, contract `status: approved`, and `approvals.jsonl` has a record with your UPN, a timestamp and the contract hash.
3. **Locked.** Call `PUT /runs/{id}/contract/kpis/Net Revenue`. **Expected:** `409 CONTRACT_LOCKED`.
4. **Role enforced.** Remove yourself from `business_owner` in `config/roles.yaml` and Accept a different run. **Expected:** `403`.
5. **Reject.** Reject a draft with the comment "Exclude tax as well". **Expected:** state `NEEDS_CLARIFICATION`, a new blocking question with `source: reviewer`, and a rejection record.
6. **Explain.** Call the `EXPLAIN` action. **Expected:** readable rationale for each KPI and question, and an approval record with action `EXPLAIN`.

---

### T-205 — Clarification and contract review UI

**Depends on:** T-204 · **Spec:** §8.1 (clarification panel, approval actions), FR-002, FR-003, FR-018 · **Fabric hosting:** ui (Nexora Run → Clarification and Contract tabs); compile and answer are jobs

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The backend part of this task (services, gates, approvals) is unchanged from the first build and only needs its operations wired through the dispatcher (hosts in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). The **UI part is ported** from `rayfin-ui/` (Next.js) to the Rayfin workload `rayfin-workload/` (T-104) as a tab of the **Nexora Run** item editor:
> - The page's display rules move over unchanged (`src/lib/review.ts` and its tests), and the page calls operations through the workload's `src/lib/api.ts` (`call` for `udf`, `runJob` for `job`, with the job progress shown).
> - Every action that is a `job` disables the run's other actions until it finishes, and shows the job's error envelope if it fails.
> - The acceptance criteria are run in Rayfin in the Fabric portal; "Rayfin" below means the workload.

#### Problem statement
Clarification, KPI editing and approval exist only as API calls. The business owner needs to do them in Rayfin, and see why approval is blocked.

#### Functional requirements
1. Run detail gets a **Compile** button and a **Clarification panel** that lists open questions with inline answer and dismiss controls.
2. A **Contract review** screen shows each KPI's formula intent, grain, filters, time logic, exclusions, currency and examples, plus dimensions, filters, security, locked facts and assumptions.
3. The approval actions: Accept, Edit (inline KPI edit), Reject (with comment), Explain (shows rationale) and Regenerate (with guidance comment).
4. The G1 result is shown as a checklist. Accept is disabled while any check fails.
5. The approval history is shown with user and time.

#### API contracts
| Workload page (first-build route) | Calls |
|---|---|
| Nexora Run → Overview, clarification panel (`/runs/[id]`) | `POST /runs/{id}/compile`, `POST …/questions/{qid}/answer`, `POST …/questions/{qid}/dismiss` |
| Nexora Run → Contract (`/runs/[id]/contract`) | `GET /runs/{id}/artifacts`, `POST /runs/{id}/gates/G1/check`, `POST /runs/{id}/approvals`, `GET /runs/{id}/approvals`, `PUT …/contract/kpis/{name}` |

The UI reads the current contract from the run detail. Add `contract` to `RunDetail` in this task.

#### Constraints
- The UI always sends `expected_hash` with edits and approvals.
- Actions the user's role can't perform are hidden, with a short note saying which role is needed.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Network error while answering | The typed answer is kept, and a retry is offered. |
| `409 STALE_VERSION` | Message: "The contract changed. Reload to see the latest version." |
| Long rationale or prompt | Collapsible sections. |
| Compile or answer job queued or running | The job progress ("Queued", "Running · <elapsed>"). Buttons are disabled. |

#### Acceptance criteria
1. **Ambiguous Revenue end to end.** In Rayfin, create a run with "Show revenue by month for 2024", compile it, answer the Revenue question, open Contract review and Accept. **Expected:** the timeline shows `CREATED → NEEDS_CLARIFICATION → CREATED → REQUIREMENT_APPROVED`, and the approval history shows your name and the time.
2. **Blocked approval is explained.** On a run with an open blocking question, open Contract review. **Expected:** Accept is disabled, and the G1 checklist shows `G1.2` failing.
3. **Explain.** Click Explain. **Expected:** the rationale is shown next to each KPI.
4. **Stale version.** Open the contract in two tabs, edit it in one, then Accept in the other. **Expected:** the stale-version message appears, and nothing is approved.
5. **M2 exit.** **Expected:** a business user can produce an approved, locked BusinessContract without leaving Rayfin.
