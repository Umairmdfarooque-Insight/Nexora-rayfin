# M3 — Data Understanding

**Exit deliverable:** validated metadata inventory. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-301 to T-303 are `Needs Fabric re-test`. T-304 (UI) was reworked and is `Not started`.

The existing code is the starting point for these tasks: `metadata_inventory.py`, `data_profiler.py`, `semantic_mapper.py`, `relationship_validator.py` and `test_product_mapping.py`. The tasks generalize that code (removing the hardcoded `saleslt` names) and attach it to a run.

---

### T-301 — Metadata Harvester per run

**Depends on:** T-204, T-103 · **Spec:** FR-005, §8.3, `MetadataInventory.json` (§10) · **Fabric hosting:** job (`POST /runs/{id}/harvest`); udf for `GET /runs/{id}/inventory`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - The OneLake table API is called as the user from the job (T-002), not through OBO.

#### Problem statement
`build_metadata_inventory` harvests one hardcoded schema from `.env` settings, and its output isn't stored anywhere. Each run needs an inventory of **its own** scope, saved as an artifact that later stages and gates can use.

#### Functional requirements
1. Harvest every table in the run's scoped Lakehouse and schemas: columns, data types, nullability and descriptions where available.
2. Save the result as `MetadataInventory.json`, versioned per harvest.
3. Record per-table errors inside the inventory, without aborting the whole harvest.
4. Re-harvesting creates a new version and invalidates any existing profiles or relationship evidence.

#### API contracts
| Method and path | Request | Response |
|---|---|---|
| `POST /runs/{id}/harvest` | `{"schemas"?: [str]}`, defaulting to `scope.schemas`, or all schemas if those are empty | `200 {"summary": {"tables": int, "columns": int, "errors": int}, "artifact": ArtifactEntry}` |
| `GET /runs/{id}/inventory` | — | `200 MetadataInventory` |

`runs/<id>/MetadataInventory.json`:
```json
{
  "schema_version": 1, "run_id": "…", "version": 1, "harvested_at": "…",
  "scope": { "workspace_id": "…", "lakehouse_id": "…", "schemas": ["saleslt"] },
  "tables": [
    {
      "schema": "saleslt", "table_name": "SalesLT_Product", "description": null,
      "columns": [ { "name": "ProductID", "data_type": "int", "nullable": false, "description": null } ],
      "profile": null, "errors": []
    }
  ],
  "relationship_evidence": [],
  "errors": [ { "table": "saleslt.Broken", "code": "FORBIDDEN", "message": "…" } ]
}
```

#### Constraints
- Read-only. Use the OneLake table API (`FabricLakehouseConnector`) and follow its paging.
- No hardcoded schema or table names in the harvester.
- Allowed in state `REQUIREMENT_APPROVED`, and in `DATA_PROFILED` for a re-harvest, which moves the run back to `REQUIREMENT_APPROVED`.
- Uses the run scope from T-103 and the user's identity from T-105.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Schema in the scope no longer exists | `404 SCHEMA_NOT_FOUND`. Nothing is written. |
| Lakehouse has 0 tables in scope | An inventory with `tables: []` and a warning. G2 (T-304) will fail. |
| One table returns `403` or an error | Recorded in `errors[]`. The other tables are still harvested. |
| Table with no column metadata | Kept, with `columns: []` and a table-level error `NO_COLUMNS`. |
| Over 500 tables | Paging handles it, and progress is logged every 50 tables. |
| Table or column name containing spaces or Unicode | Preserved exactly as returned. |
| Harvest in `CREATED` | `409 WRONG_STATE`. |

#### Acceptance criteria
1. **Harvest.** On an approved run scoped to `saleslt`, call `POST /runs/{id}/harvest`. **Expected:** `MetadataInventory.json` lists every `saleslt` table, and the summary's counts match the file.
2. **Matches the portal.** Pick three tables and compare their columns and types with the Lakehouse explorer. **Expected:** identical.
3. **Not hardcoded.** On another approved run, harvest with `{"schemas": ["nexora_test"]}`. **Expected:** the five fixture tables from T-004.
4. **Wrong state.** Harvest a run in `CREATED`. **Expected:** `409 WRONG_STATE`.
5. **Versioning.** Harvest the same run twice. **Expected:** the inventory `version` is `2`, and the artifact entry is updated.

---

### T-302 — Generic data profiler

**Depends on:** T-301 · **Spec:** FR-006, NFR-008, §14 (minimum data to the model) · **Fabric hosting:** job (`POST /runs/{id}/profile`)

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - The SQL endpoint is queried from the job as the user (T-002 token provider, with the driver T-002 chose). The 120 s statement timeout applies per query inside the job.

#### Problem statement
`profile_product_ids` only samples product IDs from two fixed tables. FR-006 requires evidence for every relevant table: row count, nulls, distinct values, key uniqueness, freshness and date coverage. That evidence must be computed without exposing raw data.

#### Functional requirements
1. Profile the **candidate** tables, meaning the tables matched to contract entities by the deterministic matcher, plus any tables the user names.
2. For each table, compute:
   - the row count
   - per-column null count and distinct count
   - uniqueness of likely key columns
   - min and max for date and numeric columns
   - freshness
3. Every profile carries `status`, `evidence` (including the SQL that was run) and `warnings`.
4. Profiles are written into the inventory's `tables[].profile`.

#### API contracts
- `app/discovery/data_profiler.py`: `profile_tables(connector, inventory, tables: list[str]) -> dict[str, TableProfile]`.
- `POST /runs/{id}/profile`, with `{"tables"?: ["schema.table"]}`, returns `200 {"profiled": int, "warnings": int, "errors": int}`.

`TableProfile`:
```json
{
  "status": "warning",
  "row_count": 295,
  "columns": {
    "ProductID": { "null_count": 0, "distinct_count": 290, "is_key_candidate": true, "is_unique": false, "min": 680, "max": 999, "approximate": false },
    "Name":      { "null_count": 0, "distinct_count": 295, "is_key_candidate": false, "is_unique": null }
  },
  "freshness": { "column": "ModifiedDate", "max_value": "2008-06-01T00:00:00Z", "status": "ok" },
  "warnings": ["ProductID is not unique (5 duplicate values)"],
  "evidence": { "queries": ["SELECT COUNT(*) …"], "profiled_at": "…" }
}
```

#### Constraints
- Run only `SELECT` statements. Quote identifiers as `[schema].[table]`, with `]` escaped. Never build SQL from free text.
- Key candidates are columns whose names end in `ID` or `Key`, case-insensitive, or that are named in a relationship candidate.
- **Data minimization:** report min and max only for date and numeric columns, never for strings. Store no row values.
- Tables over 10 million rows use `APPROX_COUNT_DISTINCT` and set `approximate: true`.
- Query timeout: 120 seconds per table.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Empty table | `row_count: 0`, a warning "table is empty", and uniqueness set to `null`. |
| Column that is all null | `null_count == row_count`, with a warning. |
| Column type that can't be counted distinctly, such as long text or binary | Skipped, with a note in the column profile. |
| Query timeout on one table | That table gets `status: error`. The others continue. |
| No date column | `freshness.status: "unknown"`. |
| Table removed since the harvest | `status: error`, `code: TABLE_NOT_FOUND`. |

#### Acceptance criteria
1. **Row count matches.** Profile a run, then in the SQL endpoint run `SELECT COUNT(*) FROM saleslt.SalesLT_Product`. **Expected:** the same number as `row_count`.
2. **Unique key.** Check `SalesLT_Product.ProductID`. **Expected:** `is_unique: true`, and `distinct_count` equals `row_count`.
3. **Duplicates detected.** Profile `nexora_test.dim_product_dupes`. **Expected:** `ProductID.is_unique: false`, `status: warning`, and a warning giving the duplicate count, which matches T-004 AC-2.
4. **Date coverage.** Compare a date column's min and max with a hand-written `SELECT MIN(col), MAX(col)`. **Expected:** they match.
5. **No raw strings.** Search the inventory file for a known product name. **Expected:** not found.

---

### T-303 — Relationship evidence

**Depends on:** T-302 · **Spec:** FR-007, §9 (Relationships row), Business rule 7 · **Fabric hosting:** job (`POST /runs/{id}/relationships/analyze`); udf for `GET /runs/{id}/relationships`

> **Fabric re-test.** Logic unchanged from the first build; status `Needs Fabric re-test`. Run the acceptance criteria below through Fabric: an operation listed as `udf` is called from Rayfin once its screen exists, else from the `nexora_api` test pane (`call(method, path, body=…)`); a `job` operation is started from Rayfin or by reserving it (`POST /runs/{id}/jobs`) and running `nexora_stage_runner` ([CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). Its response is the job result. A file named `runs/<id>/…` is in `Files/runs/<id>/…` of `nexora_control` (`python -m app.cli runs pull <id>` copies it locally). CLI steps run locally against the Fabric stores (`RUN_STORE=fabric_sql`, `ARTIFACT_STORE=onelake`).
> - AC-1's comparison script `python -m app.test_product_mapping` still runs locally.

#### Problem statement
`semantic_mapper.py` lets the LLM suggest joins, and `relationship_validator.py` checks them only partly. The spec requires every proposed relationship to carry measured evidence and a confidence level: key compatibility, value overlap, cardinality and orphan rate. LLM suggestions must stay candidates until that evidence exists.

#### Functional requirements
1. Generate candidate relationships from:
   - deterministic naming heuristics: `XxxID` in one table matched to `XxxID` or `ID` in table `Xxx`
   - the LLM semantic mapper, labelled `source: llm`
2. For each candidate, compute:
   - type compatibility
   - overlap %
   - orphan rate
   - null rate
   - cardinality
   - confidence
3. Discard any candidate that references a column not in the inventory, and give the reason.
4. Store the results in `MetadataInventory.relationship_evidence`.

#### API contracts
- `POST /runs/{id}/relationships/analyze` returns `200 {"candidates": int, "high": int, "medium": int, "low": int, "rejected": int}`.
- `GET /runs/{id}/relationships` returns `200 [RelationshipEvidence]`.

`RelationshipEvidence`:
```json
{
  "id": "R1",
  "from": { "table": "saleslt.SalesLT_SalesOrderDetail", "column": "ProductID" },
  "to":   { "table": "saleslt.SalesLT_Product", "column": "ProductID" },
  "source": "heuristic",
  "type_compatible": true,
  "overlap_pct": 100.0,
  "orphan_rate": 0.0,
  "null_rate": 0.0,
  "cardinality": "many_to_one",
  "confidence": "high",
  "thresholds": { "high": { "orphan_rate_max": 1.0, "overlap_pct_min": 99.0 }, "medium": { "orphan_rate_max": 10.0 } },
  "evidence": { "queries": ["…"], "computed_at": "…" },
  "status": "candidate",
  "rejected_reason": null
}
```

- `cardinality` is `one_to_one` | `many_to_one` | `many_to_many`.
- `confidence` is `high` | `medium` | `low`.
- `status` is `candidate` | `rejected`.
- Thresholds are configured in `config/relationship_thresholds.yaml`.

#### Constraints
- **Metadata is the source of truth.** The LLM receives only table and column names and types, plus the contract. It never receives data values.
- Evidence queries are read-only, aggregate-only, and use quoted identifiers.
- Single-column keys only. Composite keys are out of scope and noted as such.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Type mismatch, such as int to string | No join query is run. `type_compatible: false` and `confidence: low`. |
| Neither side unique | `many_to_many`, which T-403 will pick up. |
| The LLM suggests a column that doesn't exist | `status: rejected`, `rejected_reason: "column not in inventory"`. |
| Null keys | Excluded from overlap, but reported as `null_rate`. |
| Self-relationship (same table and column) | Rejected. |
| Profile missing for a table | The profile is computed first, or the candidate is rejected with "profile missing". |

#### Acceptance criteria
1. **Real join measured.** Analyze relationships on a `saleslt` run. For `SalesLT_SalesOrderDetail.ProductID → SalesLT_Product.ProductID`, compare with `python -m app.test_product_mapping`. **Expected:** the orphan rate matches, with `cardinality: many_to_one` and `confidence: high`.
2. **Bad join is low confidence.** Find a candidate with low overlap, or add one by hand such as `ProductID → CustomerID`. **Expected:** `confidence: low`, with `overlap_pct` shown.
3. **Invalid relationship evidence.** On a run including `nexora_test.dim_product_dupes`, check relationships that point to it. **Expected:** `many_to_many`, or a target that isn't unique, and the reason is visible.
4. **Hallucinated column rejected.** Inject an LLM candidate that references `Product.NoSuchColumn`. **Expected:** `status: rejected` with the reason.

---

### T-304 — Gate G2 and inventory UI

**Depends on:** T-303, T-205 · **Spec:** §12 G2, FR-005–FR-007, NFR-004, Business rule 5 · **Fabric hosting:** ui (Nexora Run → Inventory tab); harvest, profile and analyze are jobs; G2, exceptions, data gaps and approvals are udf

> **Fabric rework.** Rewritten for Fabric; status `Not started`. The backend part of this task (services, gates, approvals) is unchanged from the first build and only needs its operations wired through the dispatcher (hosts in [CONTRACTS.md#fabric-operations](CONTRACTS.md#fabric-operations)). The **UI part is ported** from `rayfin-ui/` (Next.js) to the Rayfin workload `rayfin-workload/` (T-104) as a tab of the **Nexora Run** item editor:
> - The page's display rules move over unchanged (`src/lib/inventory.ts` and its tests), and the page calls operations through the workload's `src/lib/api.ts` (`call` for `udf`, `runJob` for `job`, with the job progress shown).
> - Every action that is a `job` disables the run's other actions until it finishes, and shows the job's error envelope if it fails.
> - The acceptance criteria are run in Rayfin in the Fabric portal; "Rayfin" below means the workload.

#### Problem statement
Harvest and profiling results exist only as JSON. The spec requires a metadata and quality gate before planning. Raw or non-conformed sources need explicit exception approval, and the evidence must be visible to reviewers.

#### Functional requirements
1. Gate G2 runs automated checks on the inventory, profiles and relationship evidence.
2. A configured list of approved conformed sources is introduced here. Candidate tables outside it need an exception, with a rationale, from a `data_engineer`.
3. When G2 passes and every exception is decided, the run moves to `DATA_PROFILED`.
4. The failure actions are available: re-harvest (T-301), narrow the scope, or raise a data gap.
5. The Rayfin **Inventory** screen shows tables, column profiles, warnings and relationship evidence. Each warning links to its evidence query.

#### API contracts
- Config `config/conformed_sources.yaml`:
  ```yaml
  conformed:
    - saleslt.SalesLT_Product
    - saleslt.SalesLT_SalesOrderDetail
    - saleslt.SalesLT_SalesOrderHeader
    - saleslt.SalesLT_Customer
    - nexora_test.dim_date        # test date table from T-004
  ```

| Method and path | Request | Response |
|---|---|---|
| `POST /runs/{id}/gates/G2/check` | — | `200 GateResult` |
| `POST /runs/{id}/exceptions` | `{"table": "schema.table", "rationale": str}` (role `data_engineer`) | `201 {"exception_id", "table", "approved_by", "at"}` |
| `POST /runs/{id}/data-gaps` | `{"description": str}` | `200 RunDetail`, with the state set to `NEEDS_INPUT` |
| `POST /runs/{id}/approvals` | `{"artifact": "MetadataInventory", "action": "ACCEPT", …}` | as in T-204 |

G2 checks:

| ID | Check |
|---|---|
| G2.1 | Every candidate table is accessible, with no table errors |
| G2.2 | Every candidate has columns and types |
| G2.3 | Every candidate has a profile |
| G2.4 | Profile warnings are summarized (status `warning`, not `fail`) |
| G2.5 | Each non-conformed candidate has an approved exception |

Workload page: Nexora Run → **Inventory** (first-build route `/runs/[id]/inventory`).

#### Constraints
- Exceptions are recorded as approval records too, with artifact `Exception:<table>`.
- Only `data_engineer` can approve an exception.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| No candidate tables | G2.1 fails: "no candidate tables matched the contract". The action offered is raising a data gap. |
| An exception for a table that isn't a candidate | `422`. |
| Re-harvest after G2 passed | The run returns to `REQUIREMENT_APPROVED`, and G2 must be run again. |
| Inventory with over 200 tables in the UI | Paged list with a search box. |

#### Acceptance criteria
1. **Pass on conformed data.** On a `saleslt` run, check G2, then Accept. **Expected:** all checks pass, and the state is `DATA_PROFILED`.
2. **Exception required.** Remove `SalesLT_Customer` from `conformed_sources.yaml` and check G2 on a run whose contract needs customers. **Expected:** `G2.5` fails, naming that table.
3. **Exception approved.** As a `data_engineer`, post an exception with a rationale and re-check. **Expected:** `G2.5` passes, and the exception appears in the approvals.
4. **Evidence visible.** Open `/runs/[id]/inventory` and click a warning, for example on `dim_product_dupes`. **Expected:** the query and the numbers behind it.
5. **Data gap.** Post `/data-gaps` with a description. **Expected:** state `NEEDS_INPUT`, with the gap shown on run detail.
6. **M3 exit.** **Expected:** a validated inventory exists for the run and is reviewable in Rayfin.
