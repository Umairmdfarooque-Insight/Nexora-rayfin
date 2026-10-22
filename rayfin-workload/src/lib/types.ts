// Shapes returned by the Nexora API (see .claude/tasks/CONTRACTS.md).

export interface SqlEndpoint {
  server: string;
  database: string;
}

export interface RunScope {
  workspace_id: string;
  workspace_name: string | null;
  lakehouse_id: string;
  lakehouse_name: string;
  schemas: string[];
  sql_endpoint: SqlEndpoint | null;
}

export interface HistoryEntry {
  from: string | null;
  to: string;
  at: string;
  reason: string;
  actor: string;
}

export interface Principal {
  oid: string;
  upn: string;
  name: string;
}

export interface ActiveJob {
  job_id: string;
  operation: string;
  job_instance_id: string | null;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  started_at: string;
  finished_at: string | null;
  error: { error: { code: string; message: string; details: Record<string, unknown> } } | null;
}

export interface RunDetail {
  run_id: string;
  prompt: string;
  subject_area: string | null;
  desired_outputs: string[];
  constraints: string[];
  state: string;
  resume_state: string | null;
  scope: RunScope | null;
  created_by: Principal | null;
  created_at: string;
  updated_at: string;
  history: HistoryEntry[];
  active_job: ActiveJob | null;
  next_action: string;
  open_questions: OpenQuestion[];
  warnings: string[];
  artifacts: Record<string, ArtifactRef>;
  contract: Contract | null;
  data_gaps: DataGap[];
}

export interface DataGap {
  gap_id: string;
  description: string;
  raised_by: string;
  at: string;
  from_state: string;
}

export interface ArtifactRef {
  path: string;
  version: number;
  hash: string;
  status: string;
  updated_at: string;
}

export interface ExpectedExample {
  description: string;
  value: number | string | null;
}

export interface Kpi {
  name: string;
  formula_intent: string | null;
  grain: string | null;
  filters: string[];
  time_logic: string | null;
  exclusions: string[];
  currency: string | null;
  expected_examples: ExpectedExample[];
  status: string;
  rationale: string | null;
}

export interface ContractQuestion {
  id: string;
  text: string;
  blocking: boolean;
  reason: string | null;
  source: string;
  status: "open" | "answered" | "dismissed";
  answer: string | null;
  dismiss_reason: string | null;
}

export interface LockedFact {
  id: string;
  text: string;
  source_question_id: string | null;
  locked_at: string;
}

export interface Contract {
  run_id: string;
  version: number;
  status: "draft" | "approved";
  domain: string | null;
  business_objective: string;
  grain: string | null;
  entities: string[];
  kpis: Kpi[];
  dimensions: string[];
  filters: string[];
  currency: string | null;
  exclusions: string[];
  security: { description: string; applies_to: string[] }[];
  open_questions: ContractQuestion[];
  locked_facts: LockedFact[];
  assumptions: string[];
}

/** Fields of a KPI the owner may edit; only the fields sent change. */
export type KpiChanges = Partial<
  Pick<
    Kpi,
    "formula_intent" | "grain" | "time_logic" | "currency" | "filters" | "exclusions" | "rationale"
  >
>;

export interface GateCheck {
  id: string;
  description: string;
  status: "pass" | "fail" | "warning";
  evidence: Record<string, unknown>;
}

export interface GateResult {
  gate: string;
  artifact: string;
  artifact_hash: string;
  passed: boolean;
  checked_at: string;
  checks: GateCheck[];
}

export type ApprovalAction = "ACCEPT" | "EDIT" | "REJECT" | "EXPLAIN" | "REGENERATE";

export interface ApprovalRecord {
  approval_id: string;
  run_id: string;
  artifact: string;
  artifact_version: number;
  artifact_hash: string;
  action: ApprovalAction;
  role: string | null;
  user: { oid: string; upn: string };
  at: string;
  comment: string | null;
  gate_result_ref: string | null;
}

export interface Explanation {
  kpis: { name: string; status: string; rationale: string }[];
  questions: { id: string; text: string; rationale: string }[];
}

export interface ApprovalBody {
  artifact: "BusinessContract" | "MetadataInventory" | "ExecutionPlan" | "SemanticModel" | "Report";
  action: ApprovalAction;
  expected_hash: string;
  target?: { page?: string; visual?: string }; // the part to regenerate, for the Report (T-604)
  comment?: string;
  role?: string; // the role acted as, for the SemanticModel and the Report
  definition_wrong?: boolean; // a business_owner's Reject of the KPI definition itself
  edits?: { kpis: Record<string, KpiChanges> };
}

export interface ApprovalResult {
  approval: ApprovalRecord;
  run: RunDetail;
  gate_result: GateResult | null;
  explanation: Explanation | null;
}

export interface QuestionResult {
  run: RunDetail;
  contract: Contract;
}

export interface CompileResult extends QuestionResult {
  blocking_questions: number;
}

export interface Me {
  oid: string;
  upn: string;
  name: string;
  roles: string[];
}

export interface OpenQuestion {
  id: string;
  text: string;
  blocking: boolean;
  reason: string | null;
  source: string;
}

export interface RunSummary {
  run_id: string;
  state: string;
  prompt_preview: string;
  created_at: string;
  created_by: Record<string, unknown> | null;
}

export interface RunList {
  items: RunSummary[];
  total: number;
}

export interface Workspace {
  id: string;
  display_name: string;
  type: string;
}

export interface Lakehouse {
  id: string;
  display_name: string;
  workspace_id: string;
  sql_endpoint: SqlEndpoint | null;
}

export interface CreateRunBody {
  prompt: string;
  subject_area?: string;
  desired_outputs: string[];
  constraints: string[];
}

export interface InventoryColumn {
  name: string;
  data_type: string | null;
  nullable: boolean | null;
  description: string | null;
}

export interface ColumnProfile {
  null_count: number | null;
  distinct_count: number | null;
  is_key_candidate: boolean;
  is_unique: boolean | null;
  duplicate_count: number | null;
  min: string | number | null;
  max: string | number | null;
  approximate: boolean;
  note: string | null;
}

export interface TableProfile {
  status: "ok" | "warning" | "error";
  code: string | null;
  message: string | null;
  row_count: number | null;
  columns: Record<string, ColumnProfile>;
  freshness: {
    column: string | null;
    max_value: string | null;
    age_days: number | null;
    status: "ok" | "unknown";
  } | null;
  warnings: string[];
  evidence: { queries: string[]; profiled_at: string };
}

export interface InventoryTable {
  schema: string;
  table_name: string;
  description: string | null;
  columns: InventoryColumn[];
  profile: TableProfile | null;
  errors: { code: string; message: string }[];
}

export interface RelationshipEvidence {
  id: string;
  from: { table: string; column: string };
  to: { table: string; column: string };
  source: "heuristic" | "llm" | "user";
  type_compatible: boolean | null;
  overlap_pct: number | null;
  orphan_rate: number | null;
  null_rate: number | null;
  cardinality: "one_to_one" | "many_to_one" | "many_to_many" | null;
  confidence: "high" | "medium" | "low" | null;
  evidence: { queries: string[]; computed_at: string };
  status: "candidate" | "rejected";
  rejected_reason: string | null;
  notes: string[];
}

export interface MetadataInventory {
  run_id: string;
  version: number;
  harvested_at: string;
  scope: { workspace_id: string; lakehouse_id: string; schemas: string[] };
  tables: InventoryTable[];
  relationship_evidence: RelationshipEvidence[];
  errors: { table: string; code: string; message: string }[];
  warnings: string[];
}

export interface HarvestResult {
  summary: { tables: number; columns: number; errors: number };
  artifact: { name: string; version: number; hash: string; status: string };
  warnings: string[];
}

export interface StageCounts {
  [key: string]: number | string[];
}

export interface ExceptionResult {
  exception_id: string;
  table: string;
  approved_by: string;
  at: string;
}

export interface ScopeBody {
  workspace_id: string;
  lakehouse_id: string;
}

// --- Plan (T-404, T-405) ---

export interface ModelColumn {
  name: string;
  source_column: string;
  data_type: string;
  format_string: string | null;
  display_folder: string | null;
  is_hidden: boolean;
}

export interface ModelTable {
  name: string;
  source: { schema: string; table: string } | null;
  role: "fact" | "dimension" | "bridge" | "date";
  grain: string | null;
  columns: ModelColumn[];
  hierarchies: { name: string; levels: { name: string; column: string }[] }[];
}

export interface ModelRelationship {
  name: string;
  from: { table: string; column: string };
  to: { table: string; column: string };
  cardinality: "many_to_one" | "one_to_one" | "many_to_many";
  cross_filter: "single" | "both";
  evidence_ref: string | null;
  override_rationale: string | null;
}

export interface ModelMeasure {
  name: string;
  table: string;
  kpi: string;
  formula_intent: string | null;
  dax: string | null;
  format_string: string | null;
  display_folder: string | null;
  description: string | null;
}

export interface ModelSpec {
  run_id: string;
  version: number;
  model_name: string;
  storage_mode: "DirectLake" | "Import";
  tables: ModelTable[];
  relationships: ModelRelationship[];
  measures: ModelMeasure[];
  roles: { name: string; description: string; applies_to: string[]; table_filters: Record<string, string>[] }[];
}

export interface DataEngineeringAction {
  id: string;
  kind: "create_date_table" | "create_bridge_table";
  description: string;
  blocking: boolean;
  owner: string | null;
  status: "open" | "done";
}

export interface ExecutionPlan {
  run_id: string;
  version: number;
  sources: { table: string; role: string; conformed: boolean; exception_id: string | null }[];
  stages: { name: string; gate: string; depends_on: string[] }[];
  target: { workspace_id: string | null; kind: string };
  approvals_required: { gate: string; roles: string[] }[];
  bridges: string[];
  data_engineering_actions: DataEngineeringAction[];
  feasibility_ref: string;
}

export interface PlanResult {
  execution_plan: ExecutionPlan;
  model_spec: ModelSpec;
  validation: GateResult;
}

export interface PlanEditResult {
  execution_plan: ExecutionPlan;
  model_spec: ModelSpec;
  gate_result: GateResult;
}

export type PlanOp =
  | { op: "remove_relationship"; name: string }
  | { op: "set_storage_mode"; value: "DirectLake" | "Import" }
  | { op: "override_relationship"; name: string; rationale: string }
  | { op: "remove_measure"; name: string };

export interface SourceResolution {
  run_id: string;
  version: number;
  entities: {
    entity: string;
    selected: {
      table: string;
      score: number;
      conformed: boolean;
      exception_id: string | null;
      rationale: string;
    } | null;
    reason: string | null;
    rejected: { table: string; score: number; reason: string; requires_exception: boolean }[];
  }[];
  kpi_fields: {
    kpi: string;
    fields: { need: string; table: string; column: string; role: string }[];
    unmapped: { need: string; role: string; reason: string }[];
  }[];
}

// --- Model review (T-507) ---

export interface ModelReviewReconciliation {
  dax_value: number | null;
  sql_value: number | null;
  expected: number | null;
  status: "pass" | "fail" | "warning" | "not_validated";
  message: string | null;
}

export interface ModelReviewMeasure {
  name: string;
  table: string;
  dax: string | null;
  description: string | null;
  kpi_definition: {
    name: string;
    formula_intent: string | null;
    grain: string | null;
    filters: string[];
    time_logic: string | null;
    exclusions: string[];
    currency: string | null;
  } | null;
  reconciliation: ModelReviewReconciliation;
}

export interface RoleStatus {
  role: string;
  status: "pending" | "approved" | "rejected" | "invalidated";
  approval: ApprovalRecord | null;
  note: string | null;
}

export interface ModelApproval extends ApprovalRecord {
  current: boolean;
  note: string | null;
}

export interface ValidationCheck {
  id: string;
  object: string;
  status: "pass" | "fail" | "warning" | "skipped";
  severity: string;
  code: string | null;
  message: string;
  evidence: Record<string, unknown>;
  fingerprint: string;
}

export interface ValidationRun {
  artifact: string;
  artifact_version: number;
  artifact_hash: string;
  attempt: number;
  started_at: string;
  passed: boolean;
  repair_attempt: number | null;
  checks: ValidationCheck[];
}

export interface ModelReview {
  model_hash: string;
  version: number;
  state: string;
  tables: {
    name: string;
    role: string;
    grain: string | null;
    source: string | null;
    columns: { name: string; data_type: string; is_hidden: boolean }[];
    hierarchies: string[];
  }[];
  relationships: {
    name: string;
    from: { table: string; column: string };
    to: { table: string; column: string };
    cardinality: string;
    cross_filter: string;
    evidence_ref: string | null;
    override_rationale: string | null;
  }[];
  measures: ModelReviewMeasure[];
  roles: { name: string; description: string; applies_to: string[] }[];
  validation_report: ValidationRun | null;
  validation_current: boolean;
  gate: GateResult;
  approvals_required: string[];
  approvals_status: RoleStatus[];
  approvals_done: ModelApproval[];
}

export interface ModelGenerateResult {
  version: number;
  files: string[];
  run: RunDetail;
}

export interface MeasureResult {
  name: string;
  dax: string | null;
  status: "accepted" | "rejected";
  reason: string | null;
}

export interface MeasuresResult {
  measures: MeasureResult[];
  version: number;
}

export interface ModelValidateResult {
  report: ValidationRun;
  run: RunDetail;
}

export interface RepairAttempt {
  attempt: number;
  fixed: string[];
  remaining: string[];
  classification: Record<string, string>;
  version: number;
  discarded: boolean;
  note: string | null;
}

export interface RepairResult {
  attempts: RepairAttempt[];
  final_state: string;
  outcome: "passed" | "limit_reached" | "no_progress" | "user_input_only";
  message: string | null;
}

export interface BridgeGenerateResult {
  version: number;
  validation: ValidationRun;
}

// --- Report planner and generation (T-601, T-602, T-603) ---

export interface ReportPlanResult {
  plan: unknown; // ReportPlan.json; the review screen reads it back through getReportReview
  validation: unknown;
  run: RunDetail;
}

export interface ReportGenerateResult {
  version: number;
  files: string[];
  report_item_id: string;
  operation: "created" | "updated";
  run: RunDetail;
}

export interface ReportValidateResult {
  report: ValidationRun;
  run: RunDetail;
}

export interface RunFile {
  content_type: string;
  base64: string;
  size: number;
}

// --- Report review (T-604) ---

export type CheckStatus = "pass" | "fail" | "warning" | "skipped" | "not_validated";

export interface ReportVisualReview {
  id: string;
  title: string;
  type: string | null;
  questions: { id: string; text: string }[];
  bind_status: CheckStatus;
  query_status: CheckStatus;
  messages: string[];
}

export interface ReportPageReview {
  name: string; // the page's name in the report (p1, p2, ...)
  display_name: string | null;
  image_url: string | null; // an API path, fetched with the token; null when the page wasn't rendered
  render_status: CheckStatus;
  render_message: string | null;
  open_url: string | null;
  visuals: ReportVisualReview[];
}

export interface ReportApproval extends ApprovalRecord {
  current: boolean;
  note: string | null;
  target?: { page?: string; visual?: string } | null;
}

export interface ReportReview {
  report_hash: string;
  version: number;
  state: string;
  pages: ReportPageReview[];
  unanswered_questions: { question_id: string; reason: string }[];
  validation_report: ValidationRun | null;
  validation_current: boolean;
  gate: GateResult;
  approvals_required: string[];
  approvals_status: RoleStatus[];
  approvals_done: ReportApproval[];
}

// --- audit and trace (T-703) ---

export interface AuditEvent {
  seq: number;
  at?: string;
  type?: string;
  actor?: string;
  artifact?: string | null;
  version?: number | null;
  hash?: string | null;
  summary: string;
  prev_event_hash?: string;
  unreadable?: boolean;
}

export interface AuditPage {
  events: AuditEvent[];
  total: number;
  chain_valid: boolean;
  first_broken_seq: number | null;
}

export interface ProfileEvidence {
  null_count: number | null;
  distinct_count: number | null;
  is_key_candidate: boolean;
  is_unique: boolean | null;
  min: unknown;
  max: unknown;
  approximate: boolean;
  note: string | null;
  row_count: number | null;
  profiled_at: string;
  queries: string[];
}

export interface TraceResult {
  visual: { id: string; title: string; type: string; page: string; questions: { id: string; text: string }[] };
  measure: { name: string; dax: string | null; model_version: number | null } | null;
  other_measures: string[];
  kpi: {
    definition: {
      name: string;
      formula_intent: string | null;
      grain: string | null;
      filters: string[];
      time_logic: string | null;
      exclusions: string[];
      currency: string | null;
    } | null;
    contract_version: number | null;
    approved_by: string | null;
    approved_at: string | null;
  } | null;
  sources: { table: string; column: string; need: string; role: string; profile_evidence: ProfileEvidence | null }[];
  prompt: string;
  gaps: string[];
}
