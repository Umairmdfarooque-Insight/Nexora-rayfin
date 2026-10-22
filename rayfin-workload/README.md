# rayfin-workload

Rayfin as a Fabric workload (T-104), built with the **Fabric Extensibility
Toolkit**. This replaces `rayfin-ui/` (the first build's Next.js app on
`localhost:3000`); `rayfin-ui/` stays as reference until the last UI task
(T-703) is ported, then it is removed (CLAUDE.md "Target hosting").

## What's here

- `src/lib/` — the pure display-rule libraries (`review.ts`, `inventory.ts`,
  `plan.ts`, `model.ts`, `report.ts`, `audit.ts`, `format.ts`, `types.ts`) and
  their unit tests, ported from `rayfin-ui/src/lib/` **unchanged** (T-104
  FR-3), plus `fabricHost.ts` (the workload host client interface),
  `config.ts` (workload configuration) and `fabricRest.ts` (the T-103
  workspace/Lakehouse pickers).
- `src/lib/api.ts` — rewritten as a client of `nexora_api` and
  `nexora_stage_runner` (T-104 FR-2): `call()` invokes the `call` function of
  `nexora_api`; `runJob()` reserves a job, starts the notebook through the Job
  Scheduler API, polls the run, and returns the job's result. The `api.*`
  object keeps the same method names the pages call. T-205 added
  `dismissQuestion`, `checkG1`, `listApprovals` and `approve` (the last picks
  `call` or `runJob` by `action`, matching the dispatcher's `_approve_host`).
  T-304 added the inventory/harvest/profile/relationship/exception/data-gap
  calls, T-405 added the plan calls, and T-507 added `generateModel`,
  `addMeasures`, `validateModel`, `repairModel`, `generateBridge` and
  `getModelReview`.
- `src/pages/` — `RunsListPage` (the Rayfin home, T-104 FR-1 bullet 1),
  `NewRunPage` (the New run experience, bullet 2), `RunDetailPage` (the
  Overview tab, bullet 3, now including the Clarification panel),
  `ContractPage` (the Contract tab, T-205: KPIs, the G1 checklist, the
  approval actions and history), `InventoryPage` (the Inventory tab, T-304:
  harvest/profile/analyze, the G2 checklist, exceptions and data gaps),
  `PlanPage` (the Plan tab, T-405: resolve sources/check feasibility/generate,
  the G3 checklist, storage mode, tables, relationships and measures) and
  `ModelPage` (the Model tab, T-507: generate/add measures/validate/repair,
  the G4 checklist, tables, relationships, each measure's DAX next to its
  approved KPI definition and reconciliation, RLS roles and the per-role
  approvals). `/report`, `/audit` and `/trace` arrive with their own UI tasks
  (T-604, T-703) and are **not** in this folder yet.
- `src/components/` — `ErrorBanner`, `JobProgress`, `StateBadge` (T-104), plus
  `ClarificationPanel`, `KpiCard` and `LongText` (T-205, ported unchanged from
  `rayfin-ui/src/components/`, adapted from Next.js `Link`/routes to this
  project's `navigate` callback and tab-switch pattern), and `InventoryPanels`
  (T-304).
- `src/items/NexoraRunItem/` — the item type **Nexora Run**'s 4-file
  component pattern (`Definition`, `Editor`, `EditorEmpty`, `EditorRibbon`).
  The Editor is a `TabList` with Overview (`RunDetailPage`, T-104), Contract
  (`ContractPage`, T-205), Inventory (`InventoryPage`, T-304), Plan
  (`PlanPage`, T-405) and Model (`ModelPage`, T-507). The item stores only
  `{ runId }` (T-104 FR-5); the run itself lives in the control store.
- `src/App.tsx` — a local, router-free preview composition root (see
  "What's not included" below).
- `Manifest/` — `Product.json`, the item's `NexoraRunItem.json`/`.xml`, and a
  locale file.
- `.env.dev` / `.env.test` / `.env.prod` — per-environment workload
  configuration (home workspace id, `nexora_stage_runner` item id, the
  `nexora_api` `call`/`me` function URLs). `ITEM_NAMES=NexoraRunItem`.

## What's not included, and why

This was built against Microsoft's public documentation for the
Extensibility Toolkit and User Data Functions (see fabric-hosting.md's
platform-limits table for what's confirmed and what isn't), not against the
real **Fabric-Extensibility-Toolkit Starter-Kit** repository, which this
project doesn't have a copy of. The Starter-Kit supplies generic,
toolkit-owned scaffolding that is not safe to guess at:

- The dev server, bundler config, DevGateway and certificates
  (`scripts/Run/StartDevServer.ps1` and friends).
- The Starter-Kit's own `Workload/app/App.tsx` routing, auth bootstrap and
  `getWorkloadItem`/`saveWorkloadItem` helpers (`ItemStorage` in
  `NexoraRunItemEditor.tsx` is a documented stand-in for the latter).
  `WorkloadClientAPI`'s real shape for `acquireAccessToken`, navigation and
  notifications (`FabricHostClient` in `fabricHost.ts` is a stand-in; wire
  `setFabricHostClient()` to the real client once this project is dropped
  into the Starter-Kit).
- Tenant/publisher registration (a Workload ID in the form
  `Publisher.Workload`, the Entra app the Toolkit itself needs, and the
  Admin Portal / Workload Hub publishing steps).

**To actually run this in Fabric:** clone the Fabric-Extensibility-Toolkit
Starter-Kit, run `scripts/Setup/CreateNewItem.ps1 -ItemName NexoraRun` to
scaffold the item following its own conventions, then replace the generated
`NexoraRunItem*.tsx`/`.ts` files with the ones in `src/items/NexoraRunItem/`
(adjusting the `ItemStorage`/`FabricHostClient` call sites to the Starter-Kit's
real APIs), copy `src/lib/`, `src/components/` and `src/pages/` in as-is, and
follow the Starter-Kit's own `ITEM_NAMES`/`Product.json`/routing steps (see
the sample env files and manifests here for the values Rayfin needs).

## Commands

```
npm install
npm run typecheck   # tsc --noEmit — passes clean
npm test            # node --test "src/lib/**/*.test.ts" — the 60 ported display-rule tests pass unchanged
```

There is no `npm run dev`/`npm run build` here (no bundler is configured —
see above); those commands exist once this project is inside the Starter-Kit.

## Settings

Read by `src/lib/config.ts` from the build environment (the Starter-Kit's
per-environment env files):

| Setting | Meaning |
|---|---|
| `NEXORA_HOME_WORKSPACE_ID` | The Nexora home workspace (T-005) |
| `NEXORA_STAGE_RUNNER_ITEM_ID` | `nexora_stage_runner`'s item id, for the Job Scheduler API |
| `NEXORA_API_CALL_FUNCTION_URL` | `nexora_api`'s `call` function's public URL (enable public access in the portal, T-102) |
| `NEXORA_API_ME_FUNCTION_URL` | `nexora_api`'s `me` function's public URL |

`GET /fabric/workspaces` and `GET /fabric/workspaces/{id}/lakehouses` are
**not** Nexora operations (T-103): the pickers in `NewRunPage.tsx` call Fabric
REST directly through `fabricRest.ts`, using `FabricHostClient.acquireAccessToken`.
