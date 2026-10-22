# M0 — Baseline

Make the `app/` code reliable before building on it, both in local development **and** inside Microsoft Fabric. M0 isn't a milestone in the spec; it's groundwork for M1. Shared shapes are in [CONTRACTS.md](CONTRACTS.md); the hosting design is in [fabric-hosting.md](../technical-documentation/fabric-hosting.md).

> **Fabric rework.** T-001, T-002 and T-003 were rewritten for Fabric hosting and are `Not started` again. T-005 is new. T-004 is unchanged and stays `Done`.

---

### T-001 — Dependencies and configuration loading (Variable library, `.env` for dev)

**Depends on:** none · **Spec:** NFR-006 · **Fabric hosting:** runs in every UDF and job, and locally

#### Problem statement
The first build read every setting from `.env` in the project root. Fabric has no project root and no `.env`. A User Data Function and a notebook job get their settings from the workspace's **Variable library**, and secrets from Key Vault. The same `app.config` module has to work in all three places (local, UDF, job), without code that checks the runtime at every call site. The package list must also install into a Fabric Environment.

#### Functional requirements
1. `requirements.txt` lists every third-party package the code imports (as in the first build: `openai`, `python-dotenv`, `azure-identity`, `requests`, `pydantic`, `pyodbc`, `pyyaml`, `langgraph`, …). FastAPI and uvicorn stay only until T-102 removes `app/api/`. `pyproject.toml` builds the same code as the `nexora` wheel, which T-005 installs.
2. `app/config.py` resolves each setting in this order, and the first non-blank value wins:
   1. the process environment;
   2. the Variable library `nexora_settings`, when running in Fabric;
   3. `PROJECT_ROOT / ".env"`, locally only, loaded once.
3. `app/runtime.py` detects the runtime once: `fabric_job` (a notebook where `notebookutils` can be imported), `fabric_udf` (inside the `fabric.functions` host) or `local`. `RUNTIME` exposes it, and no other code inspects the environment.
4. Required settings are read through `get_required_setting()`. Optional settings return `None` when unset.
5. Secrets are not read from the Variable library. `AZURE_OPENAI_API_KEY` comes from Key Vault in Fabric (T-705 finishes this) and from `.env` locally. Until T-705, a job may read it from the Variable library only if `ENVIRONMENT=development`.

#### API contracts
Module `app.config`, with the same names as the first build:

| Name | Type | Required | Default |
|---|---|---|---|
| `APP_NAME` | `str` | no | `"Nexora"` |
| `UI_APP_NAME` | `str` | no | `"Rayfin"` |
| `ENVIRONMENT` | `str` | no | `"development"` |
| `AZURE_OPENAI_ENDPOINT` | `str`, with the trailing `/` stripped | yes, in jobs and locally | — |
| `AZURE_OPENAI_DEPLOYMENT` | `str` | yes, in jobs and locally | — |
| `AZURE_OPENAI_API_KEY` | `str` | yes, in jobs and locally (see FR-5) | — |
| `NEXORA_HOME_WORKSPACE_ID`, `NEXORA_CONTROL_LAKEHOUSE_ID`, `NEXORA_STAGE_RUNNER_ID` | `str \| None` | no (T-005 sets them) | `None` |
| `FABRIC_WORKSPACE_ID`, `FABRIC_LAKEHOUSE_ID`, `FABRIC_WORKSPACE_NAME`, `FABRIC_LAKEHOUSE_NAME`, `FABRIC_SQL_SERVER`, `FABRIC_SQL_DATABASE` | `str \| None` | no | `None` |

- `get_required_setting(name: str) -> str` returns the stripped value. If the value is missing or blank, it raises `ValueError("Missing required configuration: <name>. Check the nexora_settings variable library or your .env file.")`.
- `app/runtime.py`: `RUNTIME: Literal["local", "fabric_job", "fabric_udf"]`.
- Variable library `nexora_settings`: one variable per non-secret setting above, with a value set per environment (dev, test, prod).
- UDFs make no model call ([hosting rule](../technical-documentation/fabric-hosting.md#operation-hosting-rule)). In `fabric_udf` the three `AZURE_OPENAI_*` settings are therefore not required when `app.config` is imported. They become required when `app.llm.client` is imported.

#### Constraints
- Python 3.12 locally. In Fabric, the Python version of the Environment's runtime, recorded in `fabric-hosting.md`.
- Never print or log setting values. Error messages may name a setting, but never include its value.
- Don't change existing setting names.
- Importing `app.config` must not make a network call locally. In Fabric, reading the Variable library is the only call allowed.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Local run, `.env` missing | The `ValueError` names the first missing required setting. |
| Value contains only whitespace | Treated as missing. |
| `AZURE_OPENAI_ENDPOINT` has a trailing `/` | Stripped. |
| Setting is in the process environment and in another source | The process environment wins. |
| Local run from another directory | `.env` is still found through `PROJECT_ROOT`. |
| Fabric run, Variable library not attached or a variable missing | `ValueError` naming the setting and `nexora_settings`. |
| Fabric UDF imports a module that imports `app.llm.client` | `ValueError` naming `AZURE_OPENAI_ENDPOINT`, because a UDF must not call the model. |

#### Acceptance criteria
1. **Clean install.** Create a new virtualenv and run `pip install -r requirements.txt`, then `pip wheel . --no-deps -w dist`. **Expected:** both succeed, and `dist/` holds `nexora-*.whl`.
2. **Defaults load locally.** Run `python -c "import app.config as c; from app.runtime import RUNTIME; print(c.APP_NAME, RUNTIME)"`. **Expected:** `Nexora local`.
3. **Missing setting.** Comment out `AZURE_OPENAI_API_KEY` in `.env` and run `python -c "import app.llm.client"`. **Expected:** `ValueError: Missing required configuration: AZURE_OPENAI_API_KEY…`, with no value shown. Restore the line afterwards.
4. **Whitespace value.** Set `AZURE_OPENAI_DEPLOYMENT=   ` and repeat AC-3. **Expected:** the same error, naming `AZURE_OPENAI_DEPLOYMENT`. Restore it afterwards.
5. **Directory-independent.** From `C:\`, run `python -c "import sys; sys.path.insert(0, r'<project>'); import app.config as c; print(c.ENVIRONMENT)"`. **Expected:** the value from `.env`.
6. **Variable library in a job.** In a notebook of the home workspace, with Environment `nexora_env` and the `nexora_settings` library attached, run `from app.runtime import RUNTIME; import app.config as c; print(RUNTIME, c.ENVIRONMENT)`. **Expected:** `fabric_job` and the library's value for the active environment. This criterion needs T-005; if T-005 isn't done yet, run it during T-005's manual test.
7. **Imports work.** Run `python -c "import app.test_llm, app.test_fabric_sql, app.test_metadata_inventory"`. **Expected:** no `ImportError`.

---

### T-002 — One token provider for Fabric jobs, functions and local dev

**Depends on:** T-001 · **Spec:** NFR-001 · **Fabric hosting:** used by every job; UDFs need no source tokens

#### Problem statement
The first build used one process-wide `InteractiveBrowserCredential` locally, and OBO in the API to act as the user. Neither applies in Fabric. A notebook job already runs as the user who started it, and gets that user's tokens from `notebookutils.credentials.getToken`. Every connector (OneLake, SQL endpoint, Fabric REST, Power BI) must take its token from one provider that does the right thing for the runtime, so that the stage code doesn't change.

#### Functional requirements
1. `app/auth.py` keeps `get_token(scope) -> str` and the scope constants. It picks the provider from `app.runtime.RUNTIME`:
   - `local`: the shared `InteractiveBrowserCredential` with its on-disk cache and sign-in record, as in the first build.
   - `fabric_job`: `notebookutils.credentials.getToken(<audience>)`, with each scope mapped to its notebookutils audience (storage, Power BI, SQL, Fabric). The token is cached until 5 minutes before it expires.
   - `fabric_udf`: no source tokens. `get_token` raises `RuntimeError("Source access is not allowed in a User Data Function; run this operation as a job")`.
2. Connectors keep their optional `token_provider` argument, which defaults to `app.auth.get_token`.
3. No OBO in Fabric. The first build's `get_sql_connector_factory`, `get_workspace_lister`, `get_promotion_items_client` and the Power BI client take the job-context provider instead. `api/obo.py` is removed together with `app/api/` in T-102.
4. `FabricSQLConnector` works in a job.
   - If the ODBC Driver 18 and `pyodbc` are missing from the notebook runtime, the connector falls back to a driver that is available (for example `mssql-python`). The access token is passed the same way and the read-only rules stay.
   - The driver chosen and the reason go in the platform-limits table of `fabric-hosting.md`.

#### API contracts
Module `app.auth`:
- `STORAGE_SCOPE = "https://storage.azure.com/.default"`, `SQL_SCOPE = "https://database.windows.net/.default"`, `FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"`, `POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"`
- `get_token(scope: str) -> str`
- `get_credential()`: locally only. In Fabric it raises `RuntimeError`.
- `current_identity() -> {oid, upn, name} | None`: in a job, the user the job runs as; locally, `None`.

#### Constraints
- Never write a token to logs, files, notebook output or the run store.
- Never create a credential anywhere else in the code.
- The connectors' public methods keep their signatures.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Local run: the user closes the browser sign-in | `ClientAuthenticationError` with a readable message. |
| Local run: the encrypted cache is unavailable | In-memory cache and one warning. |
| Job: `getToken` fails (no consent, unsupported audience) | `403 CONSENT_REQUIRED` naming the scope, never the token. |
| A token expires during a long job | The provider refreshes it before the next call. |
| A UDF calls `get_token` | `RuntimeError`, as in FR-1. |

#### Acceptance criteria
1. **Local, one sign-in per process.** Run a scratch script that calls `build_metadata_inventory(FabricLakehouseConnector(), "saleslt")` and then `profile_product_ids(FabricSQLConnector())`. **Expected:** the browser sign-in appears once. Run it again within an hour. **Expected:** no sign-in prompt.
2. **Job, no prompt.** In a home-workspace notebook (Environment `nexora_env`), run `from app.auth import get_token, STORAGE_SCOPE, SQL_SCOPE; print(bool(get_token(STORAGE_SCOPE)), bool(get_token(SQL_SCOPE)))`. **Expected:** `True True`, and no token is printed.
3. **Job acts as the user.** In the same notebook, list the tables of a Lakehouse you can read, then of one you can't. **Expected:** the first works and the second fails with `FORBIDDEN`.
4. **SQL in a job.** In the notebook, run `SELECT 1 AS x` through `FabricSQLConnector` on the sandbox SQL endpoint. **Expected:** one row, and the driver used is recorded in `fabric-hosting.md`.
5. **UDF refuses.** In the `nexora_api` test pane, call a test function that calls `get_token(STORAGE_SCOPE)`. **Expected:** the `RuntimeError` message from FR-1.

---

### T-003 — Environment smoke check (local and as a Fabric job)

**Depends on:** T-002 · **Spec:** NFR-007 · **Fabric hosting:** CLI locally; `nexora_stage_runner` with `operation = smoke` in Fabric

#### Problem statement
There is no quick way to tell whether a configuration works: the settings, the LLM endpoint, the Lakehouse, the SQL endpoint, and in Fabric the control store and the Variable library. Developers find out only when a stage fails part-way.

#### Functional requirements
1. One command runs these independent checks:
   - **llm:** the model returns a reply.
   - **lakehouse:** tables can be listed.
   - **sql:** `SELECT 1` runs on the source SQL endpoint.
   - **control:** `SELECT 1` runs on `nexora_control_db`, and a file can be written and deleted under `Files/runs/_smoke/` of `nexora_control`. Skipped locally when `RUN_STORE=file`.
   - **settings:** every required setting resolves. The check names each setting's source (environment, Variable library or `.env`), never its value.
2. Each check reports `PASS`, `FAIL` or `SKIP`, with its duration and a short reason. One failing check doesn't stop the others.
3. The same checks run as a Fabric job. The stage runner with `operation = smoke` prints the text report in the notebook output and returns the JSON as the notebook's exit value.

#### API contracts
- CLI: `python -m app.tools.smoke [--only llm,lakehouse,sql,control,settings] [--json]`. Exit `0` when every check passes or skips, `1` if any fails, `2` for a configuration error or a bad `--only`.
- Job: `nexora_stage_runner` with the parameter `operation = "smoke"` (optional `only`). Exit value: `{"passed": bool, "checks": [{"name", "status", "duration_ms", "detail"}]}`.

#### Constraints
- Read-only, apart from the control check's own temporary file.
- The LLM check uses at most 20 tokens.
- 30-second timeout per check.
- No setting values or tokens in the output.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| A required setting is missing | Locally: exit `2` naming it. In a job: the `settings` check fails naming it. |
| A check times out | `FAIL … timed out after 30s`. The others still run. |
| Unknown name in `--only` | Exit `2`, listing the valid names. |
| `FABRIC_LAKEHOUSE_NAME` unset | The lakehouse check fails, naming the setting. |
| The user can't write to the control Lakehouse | `FAIL control` with `FORBIDDEN`. |

#### Acceptance criteria
1. **All pass locally.** Run `python -m app.tools.smoke`. **Expected:** `PASS` for llm, lakehouse, sql and settings, `SKIP control` (with `RUN_STORE=file`), and exit `0`.
2. **One failure is isolated.** Set `FABRIC_SQL_SERVER=invalid.example.com` and run it again. **Expected:** `FAIL sql` with a readable reason, the other checks unchanged, and exit `1`.
3. **Single check.** Run `python -m app.tools.smoke --only llm --json`. **Expected:** JSON containing only the `llm` check.
4. **Bad option.** Run `python -m app.tools.smoke --only foo`. **Expected:** exit `2`, listing the valid names.
5. **In Fabric.** After T-005, run `nexora_stage_runner` from the portal with `operation = smoke`. **Expected:** every check passes, `control` included, and the `settings` lines name the Variable library as the source.

---

### T-004 — Test fixtures in the sandbox Lakehouse

**Depends on:** T-003 · **Spec:** §6 Acceptance Scenarios, §15 · **Fabric hosting:** already a Fabric notebook; not affected by the rework (stays `Done`)

#### Problem statement
The acceptance scenarios need data that deliberately breaks rules: duplicate dimension keys, a many-to-many relationship with a mapping table, and one without a mapping table. The `saleslt` sample has none of these, so the scenarios can't be tested. `saleslt` also has no date table, and Direct Lake models can't create one as a calculated table. The monthly KPIs in the scenarios therefore need a physical date table.

#### Functional requirements
1. A Spark notebook creates the fixture tables in schema `nexora_test` of the **sandbox** Lakehouse.
2. The tables use `ProductID` values that exist in `saleslt.SalesLT_Product`, so they can join to the real data.
3. Rerunning the notebook recreates the tables with the same content.
4. `fixtures/README.md` documents each table and the scenario it supports.

#### API contracts
Files: `fixtures/create_test_tables.ipynb` (PySpark) and `fixtures/README.md`.

| Table | Columns | Required property | Used by |
|---|---|---|---|
| `nexora_test.dim_product_dupes` | `ProductID int, Name string, Category string` | At least 3 `ProductID` values appear more than once | "Invalid relationship" (T-302, T-303, T-504) |
| `nexora_test.dim_promotion` | `PromotionID int, PromotionName string, StartDate date, EndDate date` | 3–5 promotions with unique IDs | "Many-to-many bridge" (T-403, T-506) |
| `nexora_test.bridge_product_promotion` | `ProductID int, PromotionID int` | Unique `(ProductID, PromotionID)` pairs. Some products have more than one promotion, and some promotions cover more than one product. | "Many-to-many bridge" |
| `nexora_test.dim_campaign` | `CampaignID int, CampaignName string, ProductCategory string` | Relates to many products **only** through free-text category. No mapping table exists. | "Unsupported bridge" (T-403) |
| `nexora_test.dim_date` | `Date date, Year int, Quarter string, MonthNumber int, MonthName string` | One row per day, covering every `OrderDate` in `saleslt.SalesLT_SalesOrderHeader`. `Date` is unique. | Monthly KPIs and the Calendar hierarchy (T-404, T-502, T-601) |

#### Constraints
- **The Lakehouse SQL endpoint is read-only.** Tables must be written with Spark (`saveAsTable`, mode `overwrite`), not SQL `CREATE TABLE`.
- Write only to the sandbox Lakehouse and the `nexora_test` schema. Never write to `saleslt`.
- Use small, synthetic data: 100 rows or fewer per table (except `dim_date`), and no real customer data.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| Schema `nexora_test` doesn't exist | The notebook creates it first. |
| Lakehouse doesn't have schemas enabled | The notebook stops with a clear message, because schema-enabled Lakehouses are required. |
| Notebook rerun | The tables are overwritten, with no duplicate rows. |

#### Acceptance criteria
1. **Tables exist.** Run the notebook in the sandbox Lakehouse. **Expected:** the five tables appear under `nexora_test` in the Lakehouse explorer.
2. **Duplicates present.** In the SQL endpoint, run `SELECT ProductID, COUNT(*) c FROM nexora_test.dim_product_dupes GROUP BY ProductID HAVING COUNT(*) > 1`. **Expected:** at least 3 rows.
3. **Bridge is unique.** Run `SELECT ProductID, PromotionID, COUNT(*) FROM nexora_test.bridge_product_promotion GROUP BY ProductID, PromotionID HAVING COUNT(*) > 1`. **Expected:** 0 rows.
4. **Keys join to real data.** Run `SELECT COUNT(*) FROM nexora_test.bridge_product_promotion b LEFT JOIN saleslt.SalesLT_Product p ON b.ProductID = p.ProductID WHERE p.ProductID IS NULL`. **Expected:** `0`.
5. **Date coverage.** Run `SELECT COUNT(*) FROM saleslt.SalesLT_SalesOrderHeader h LEFT JOIN nexora_test.dim_date d ON CAST(h.OrderDate AS date) = d.Date WHERE d.Date IS NULL`. **Expected:** `0`.
6. **Rerunnable.** Run the notebook again, then repeat AC-2 and AC-3. **Expected:** the same results, and the row counts are unchanged.
7. **Visible to the harvester.** Point `app/test_metadata_inventory.py` at schema `nexora_test` and run it. **Expected:** all five tables are listed with their columns.

---

### T-005 — Nexora home workspace and Fabric packaging

**Depends on:** T-001, T-003 · **Spec:** NFR-003, NFR-006, NFR-009 · **Fabric hosting:** new task; creates the items every later task runs in

#### Problem statement
On Fabric, Nexora is a set of items in its own workspace, not a process on a laptop. Those items have to exist and be created the same way in every environment, and they carry the code and configuration:
- a function item and a stage-runner notebook;
- an Environment with the `nexora` wheel;
- the control SQL database and the control Lakehouse;
- a Variable library and an Eventhouse.

Until they exist, no later task can run in Fabric.

#### Functional requirements
1. A **home workspace** per environment (`Nexora-dev`, `Nexora-test`, `Nexora-prod`), on a Fabric capacity, holds these items:

   | Item | Type | Purpose |
   |---|---|---|
   | `nexora_env` | Environment | Custom library: the `nexora` wheel, plus `requirements.txt` |
   | `nexora_api` | User Data Functions | Short operations (T-102) |
   | `nexora_stage_runner` | Notebook, attached to `nexora_env`, default Lakehouse `nexora_control` | Job operations (T-102). Parameters `job_id`, `run_id`, `operation`, `payload` |
   | `nexora_control_db` | SQL Database | Run manifests, locks, access cache |
   | `nexora_control` | Lakehouse | `Files/runs/`, `Files/config/` |
   | `nexora_settings` | Variable library | Non-secret settings per environment |
   | `nexora_telemetry` | Eventhouse | Telemetry (T-704) |

2. **Packaging.** `python -m app.tools.package`:
   - builds the wheel, versioned `0.<git commit count>.0+<short sha>`;
   - copies `config/*.yaml` into `build/config/`;
   - writes `build/manifest.json` with the wheel name, the wheel hash and the config file hashes.
3. **Deployment.** `python -m app.tools.deploy_home --env dev` creates or updates the items above from the definitions committed under `fabric/` (`fabric/nexora_api/`, `fabric/nexora_stage_runner.Notebook/`, `fabric/nexora_settings.VariableLibrary/`, …), through the Fabric REST API or `fabric-cicd`. It then:
   - uploads the wheel to `nexora_env` and publishes the Environment;
   - uploads `config/*.yaml` to `Files/config/`;
   - writes the ids of the items it created into the Variable library: `NEXORA_HOME_WORKSPACE_ID`, `NEXORA_CONTROL_LAKEHOUSE_ID`, `NEXORA_STAGE_RUNNER_ID`, `FABRIC_CONTROL_DB_SERVER` and `FABRIC_CONTROL_DB_NAME`.

   Running it again with nothing changed changes nothing. Connecting the home workspace to the repo with Fabric Git integration is an allowed alternative. Record the choice in `fabric-hosting.md`.
4. Deployment is allowed only into a workspace whose name starts with `Nexora-`. That workspace must be the one in `NEXORA_HOME_WORKSPACE_ID`, or, the first time, the one the user names with `--create`.

#### API contracts
- `fabric/` folder: one sub-folder per item, in the Fabric Git format.
- `python -m app.tools.package`: exit `0`, or `1` on failure.
- `python -m app.tools.deploy_home --env dev|test|prod [--create NAME] [--dry-run]`: prints one line per item, `created`, `updated` or `unchanged`. Exit `0`, `1` for a Fabric error, `2` for bad arguments.
- `fabric-hosting.md`: fill in the wheel and Environment rows of the platform-limits table.

#### Constraints
- Never deploy into a source, sandbox or target workspace. The home workspace holds only Nexora's own items.
- No secret goes into `fabric/`, the Variable library, the wheel or the notebook. The model key comes from Key Vault (T-705).
- Deterministic: the same commit gives the same wheel hash and the same item definitions.

#### Edge cases and error handling
| Case | Expected behavior |
|---|---|
| The workspace has no capacity assigned | Exit `1` with "assign a Fabric capacity to <workspace>". |
| An item with the same name but another type exists | Exit `1` naming it (`NAME_CONFLICT`). Nothing else changes. |
| Publishing the Environment fails or times out | Exit `1` with Fabric's error. The previously published Environment stays in use. |
| The target workspace isn't a `Nexora-` workspace | Exit `2`. Nothing is deployed. |
| Rerun with no changes | Every item is `unchanged`. |

#### Acceptance criteria
1. **Package.** Run `python -m app.tools.package` twice. **Expected:** the same wheel hash both times, and `build/config/` holds every `config/*.yaml`.
2. **Create.** Run `python -m app.tools.deploy_home --env dev --create Nexora-dev`. **Expected:** the seven items appear in the workspace, and the Environment shows the `nexora` wheel as published.
3. **Idempotent.** Run `deploy_home --env dev` again. **Expected:** every line says `unchanged`.
4. **Smoke in Fabric.** Run `nexora_stage_runner` from the portal with `operation = smoke`. **Expected:** the T-003 checks pass, `control` included. Also run T-001 AC-6.
5. **Config copied.** Open `nexora_control` → `Files/config/`. **Expected:** `roles.yaml`, `glossary.yaml` and the other config files are there.
6. **Guard.** Run `deploy_home --env dev --create MyWorkspace`. **Expected:** exit `2`, and nothing is created.
