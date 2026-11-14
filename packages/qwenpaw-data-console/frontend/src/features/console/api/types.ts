export type SessionStatus = 'idle' | 'running';
export type ChatStatus = 'created' | 'running' | 'completed' | 'failed' | 'canceled';
export type ResponseStatus = 'created' | 'in_progress' | 'completed' | 'failed' | 'cancelled';

export interface ApiErrorBody {
  code: 'UNAUTHORIZED' | 'FORBIDDEN' | 'NOT_FOUND' | 'CONFLICT' | 'VALIDATION';
  message: string;
  details: Record<string, unknown> | null;
}

// Shared by Chat snapshots and failed response events. Keep unknown codes
// compatible with newer servers; only known actionable codes drive UI links.
export interface ChatError {
  code: string;
  message: string;
  details?: { cause?: string } | null;
}

export interface AttachmentRef {
  attachment_id: string;
  filename: string;
}

export interface DatasetOption {
  id: string;
  name: string;
  description: string;
}

export interface DatasourceOption {
  id: string;
  name: string;
  status: string;
  description: string;
  recommended: boolean;
}

export interface ProviderModel {
  id: string;
  name: string;
  source: 'catalog' | 'extra' | 'override';
  context_size: number | null;
  thinking_enabled: boolean | null;
  generate_kwargs: Record<string, unknown>;
  chat_model: string | null;
}

export interface ModelProvider {
  id: string;
  name: string;
  chat_model: string;
  api_key_prefix: string;
  catalog_base_url: string;
  base_url: string;
  configured: boolean;
  api_key_masked: string;
  models: ProviderModel[];
}

export interface ActiveModels {
  default_provider_id: string | null;
  default_model_id: string | null;
  light_provider_id: string | null;
  light_model_id: string | null;
}

export type ToolConfirmationLevel = 'off' | 'auto' | 'all';
export type ToolConfirmationDecision = 'approved' | 'denied';

export interface PendingToolConfirmation {
  callId: string;
  chatId: string;
  name: string;
  argumentsText: string;
  expiresAt: string | null;
}

export interface RuntimeSettings {
  tool_confirmation_level?: ToolConfirmationLevel;
  tool_timeout_enabled: boolean;
  tool_timeout_seconds: number;
  react_max_iters: number;
  llm_retry_enabled: boolean;
  llm_max_retries: number;
  context_trigger_ratio: number;
  context_reserve_ratio: number;
  tool_result_limit: number;
}

export interface ContextUsage {
  context_size: number;
  used_tokens: number;
  trigger_ratio: number;
  reserve_ratio: number;
  tool_result_limit: number;
}

/** Session sharing always requires a logged-in identity, including anyone. */
export type SessionShareScope = 'none' | 'workspace' | 'tenant' | 'anyone';

export interface ConsoleSession {
  id: string; // Persistent session ID.
  agent_id: string; // Agent associated with this session.
  title: string; // Session display title.
  status: SessionStatus; // Current session execution state.
  datasource_id: string | null; // Datasource selected for the session.
  chat_count: number; // Number of chats in the session.
  channel: string; // Channel that created the session.
  parent_session_id: string | null; // Source session for a linked review session.
  forked_from_chat_id: string | null; // Source chat for a forked session.
  created_at: string; // Session creation timestamp.
  updated_at: string; // Most recent session update timestamp.
  owner_user_id?: string; // Absent on older backends; user ID alone does not prove write access.
  share_scope?: SessionShareScope; // Absent on older backends; display as none without granting access.
  enable_clarification: boolean; // false: this session skips ask-user questions until turned back on.
}

export interface PatchConsoleSessionRequest {
  title?: string | null; // Omitted or null keeps the saved title unchanged.
  share_scope?: SessionShareScope | null; // Omitted or null keeps the saved sharing scope unchanged.
  enable_clarification?: boolean | null; // Omitted or null keeps the saved clarification setting unchanged.
}

/**
 * Report reproduction artifacts are plain files under
 * `report-reproduction/` in the session artifact dir, written by the skill's
 * own `scripts/report_cli.py`. The host serves them through the generic
 * `GET /sessions/{sid}/files/access` endpoint, so every interface below mirrors
 * a file on disk rather than a REST response body.
 */

export type ReportSectionStatus =
  'pending' | 'running' | 'needs_input' | 'validating' | 'approved' | 'failed';

export type ReportRunSectionStatus =
  'pending' | 'running' | 'needs_input' | 'checking' | 'approved' | 'failed';

/** `runs/{period}/run_manifest.json` (`report_cli.py:ReportRunManifest`). */
export interface ReportRunManifest {
  schema_version: number;
  revision: number;
  report_id: string;
  period: string;
  baseline_revision: number;
  recipe_hash: string;
  created_at: string;
  updated_at: string;
  sections: Array<{
    section_id: string;
    status: ReportRunSectionStatus;
    attempt: number;
    updated_at: string;
    error: string | null;
  }>;
}

/** `report_manifest.json` → `sections[]` (`report_cli.py:SectionProgress`). */
export interface ReportManifestSection {
  section: {
    section_id: string;
    title: string;
    level: number;
    /** All four paths are relative to the `report-reproduction/` product dir. */
    source_path: string;
    draft_path: string;
    spec_path: string;
    /**
     * Where `compare-section` writes the machine verdict and
     * `apply-comparison` the human review. Renamed from `validation_path` in
     * manifest schema version 3.
     */
    comparison_path: string;
    blocked_by: string[];
  };
  status: ReportSectionStatus;
  attempt: number;
  updated_at: string;
  error: string | null;
}

/** `report-reproduction/report_manifest.json` (`report_cli.py:ReportManifest`). */
export interface ReportManifest {
  schema_version: number;
  revision: number;
  report_id: string;
  title: string;
  /** GT document, relative to the product dir, e.g. `source/original.html`. */
  source_path: string;
  source_period: string | null;
  target_period: string | null;
  baseline_frozen_at: string | null;
  created_at: string;
  updated_at: string;
  sections: ReportManifestSection[];
}

/** `schedule_descriptor.json`, written by `report_cli.py freeze-recipe`; the `schedule-descriptor` subcommand only refreshes it. */
export interface ReportScheduleDescriptor {
  report_id: string;
  title: string;
  recipe_hash: string;
  /** Inferred cron period shape; absent in descriptors written before it existed. */
  period?: {
    unit: 'week' | 'month';
    offset: number;
    window_size: number;
    label_format: string;
  };
  parameters: Record<string, string>;
}

/**
 * One reference number the reproduced draft did not match
 * (`agent/tools/compare.py:NumericDifference`).
 */
export interface NumericDifference {
  reference: string; // Reference number as source text.
  target: string | null; // Null when the reference number has no target counterpart.
  relative_error: number | null; // Relative numeric error calculated by the backend.
  matched: boolean; // Backend verdict for this number.
  note: string | null; // Reviewer explanation written by apply-comparison.
  target_start: number | null; // Inclusive, zero-based Unicode code point offset in the full target.
  target_end: number | null; // Exclusive Unicode code point offset; not a JS UTF-16 index.
}

export interface StructureDifference {
  kind: string; // Backend-defined structural difference category.
  description: string; // Human-readable difference description.
  severity: 'info' | 'warning' | 'error'; // Display severity supplied by the backend.
}

/** One 口径 judgement passed to `compare-section --criteria`. */
export type CriteriaAnalysisItem = Record<string, unknown>;

/**
 * The human-review trail of one comparison. Only
 * `apply-comparison --approved-by-user --reason …` writes it, so its presence is
 * the auditable proof that a user — not the agent — accepted a remaining gap.
 */
export interface ComparisonReview {
  approved_by_user: boolean; // Whether a user accepted the remaining differences.
  reason: string; // User-provided reason for acceptance.
  reviewed_at: string; // ISO timestamp recorded by the backend.
}

/** Pure, immediate result of POST files/compare; no review record is returned. */
export interface ComparisonResult {
  target_html_spans?: { id: string; start: number; end: number }[]; // Code-point spans in original HTML, not normalized Markdown.
  schema_version: number; // Backend result schema version.
  target_path: string | null; // Target path relative to the artifact directory.
  reference_path: string | null; // Reference path relative to the artifact directory.
  passed: boolean; // Final backend verdict; the frontend must not recalculate it.
  passed_with_review: boolean; // Whether the verdict includes user acceptance.
  numeric_coverage: number; // Reference numeric coverage reported by the backend.
  numeric_differences: NumericDifference[]; // Numeric comparisons with full-target offsets.
  structure_differences: StructureDifference[]; // Structural differences reported by the backend.
  criteria_analysis: CriteriaAnalysisItem[]; // Arbitrary criterion objects, without fixed child fields.
  notes: string; // Additional notes, preserving line breaks.
  tolerance: number; // Relative numeric tolerance in the range 0..1.
  created_at: string; // ISO timestamp recorded by the backend.
}

export interface ComparisonEvidenceGate {
  passed: boolean; // Backend evidence validation verdict.
  provenance: string; // Evidence provenance recorded by the backend.
  violations: string[]; // Evidence validation violations.
  checked_at: string; // ISO timestamp of evidence validation.
}

/** Persisted sections/{id}/baseline/comparison.json, including read-only review. */
export interface ComparisonFile extends ComparisonResult {
  review: ComparisonReview | null; // Persisted user acceptance record.
  evidence_gate: ComparisonEvidenceGate | null; // Evidence check already included in passed.
}

/**
 * One compare suggestion from `POST /sessions/{sid}/files/compare/recommendations`.
 * The reference comes from candidates; target is current_path when provided
 * and does not need to appear in candidates. Both paths are artifact-dir relative.
 */
export interface CompareRecommendationItem {
  key: string; // Stable recommendation identifier.
  label: string; // Recommendation title.
  description: string | null; // Optional explanation.
  reference_path: string; // Reference selected from the supplied candidates.
  target_path: string; // Current preview path when current_path is supplied.
}

/**
 * `sections/{id}/runs/{period}/run_checks.json`
 * (`report_cli.py:SectionRunChecks`) — a cross-period run has no GT to compare
 * against, so it carries business checks instead of numeric differences.
 */
export interface RunChecksFile {
  report_id: string;
  period: string;
  section_id: string;
  passed: boolean;
  checks: Array<{ name: string; passed: boolean; details: string }>;
  notes: string;
}

export interface Artifact {
  id: string;
  session_id: string;
  chat_id: string | null;
  name: string;
  path: string;
  created_at: string;
  updated_at: string;
}

export interface ArtifactComment {
  path: string;
  line_start: number;
  line_end: number;
  comment: string;
}

export type ArtifactAccessPurpose = 'preview' | 'download';

export interface ArtifactShare {
  url: string;
  expires_at: string;
  name: string;
}

export type TaskState = 'pending' | 'in_progress' | 'completed';

export interface PlanTask {
  id: string;
  subject: string;
  description: string;
  metadata: Record<string, unknown>;
  created_at?: string;
  state: TaskState;
  owner: string | null;
  blocks: string[];
  blocked_by: string[];
}

/** Chat-level Plan projection; null when tasks is empty. */
export interface Plan {
  tasks: PlanTask[];
}

export interface Presentation {
  card_type: 'user' | 'thinking' | 'text' | 'hint' | 'tool';
  caption: string;
  body: string;
}

export interface BizEvent {
  timing?: ExecutionTiming;
  event_id: string;
  seq: number;
  channel: 'main' | 'subagent';
  block_id: string | null;
  status: 'done' | 'error';
  presentation: Presentation | null;
  started_at: number;
  ended_at: number | null;
}

export interface SegmentCoverage {
  start_seq: number;
  end_seq: number;
}

export interface SegmentArtifact {
  name: string;
  description: string;
  relative_path: string;
}

export interface SessionSegmentArtifact extends SegmentArtifact {
  chat_id: string;
  segments: Array<{ segment_id: string; chat_id: string; title: string }>;
}

export interface SegmentConfidence {
  level: 'high' | 'medium' | 'low'; // 后端给出的整体置信度档位，不是数值分数。
  summary: string; // 完整定档原因，前端不截断或推导评分。
}

export interface Segment {
  workedForSeconds?: number | null;
  timingTotals?: { modelSeconds: number; toolSeconds: number };
  segment_id: string;
  chat_id: string | null;
  title: string;
  input: string | null;
  behavior: string;
  conclusion: string;
  confidence?: SegmentConfidence | null; // 旧数据及评分失败可能缺省或为 null，均不展示徽标。
  artifact: SegmentArtifact[] | null;
  coverage: SegmentCoverage;
  started_at: number | null;
  ended_at: number | null;
}

export interface AskUserQuestionOption {
  label: string;
  description: string | null;
}

export interface AskUserQuestionQuestion {
  question: string;
  description: string;
  multiSelect: boolean;
  options: AskUserQuestionOption[];
  required: boolean;
  allowCustomText: boolean;
}

export interface PendingAskUserQuestion {
  id: string;
  chat_id: string | null;
  title: string;
  questions: AskUserQuestionQuestion[];
  expires_at: string | null;
}

export interface AskUserQuestionSemanticAnswer {
  question: string;
  selected_options: string[];
  custom_text: string | null;
}

export type AskUserQuestionToolResult =
  | { status: 'answered'; answers: AskUserQuestionSemanticAnswer[] }
  | { status: 'timeout'; reason: string };

export interface FollowUp {
  chat_id: string | null;
  questions: string[];
}

export interface MessageContent {
  object: 'content';
  type: 'text' | 'image' | 'audio' | 'video' | 'data' | 'file' | 'refusal';
  delta: boolean;
  index: number | null;
  status: string | null;
  msg_id: string | null;
  text: string | null;
  image_url: string | null;
  data: unknown;
  format: string | null;
  video_url: string | null;
  filename: string | null;
  file_url: string | null;
  refusal: string | null;
}

export interface TraceMessage {
  timing?: ExecutionTiming;
  id: string;
  object: 'message';
  chat_id: string | null;
  sequence: number;
  type: string;
  role: 'user' | 'assistant' | 'system' | 'tool' | null;
  content: MessageContent[];
  status: 'created' | 'in_progress' | 'completed' | 'failed' | 'cancelled';
  source_id?: string | null;
  metadata: Record<string, unknown> | null;
}

export interface ConsoleChat {
  enable_clarification?: boolean; // Older servers default to manual clarification.
  id: string;
  session_id: string;
  sequence: number;
  user_input: string;
  datasource_id: string | null;
  dataset_names?: string[] | null;
  kind: 'simple' | 'planned';
  status: ChatStatus;
  forkable: boolean;
  last_sequence_number: number;
  started_at: string | null;
  completed_at: string | null;
  active_duration_ms: number;
  error: ChatError | null;
  plan: Plan | null;
  segments: Segment[];
  biz_events: BizEvent[];
  artifacts: Artifact[];
  artifact_comments?: ArtifactComment[];
  followup: FollowUp | null;
  attachments?: AttachmentRef[];
}

interface StreamBase {
  created_at?: string; // Absent in older stored events/servers.
  sequence_number: number;
  session_id: string;
  chat_id: string;
}

export interface McpConnectEvent extends StreamBase {
  object: 'mcp.connect';
  mcp_name: string;
  status: 'connected' | 'failed';
  error?: string | null;
  url?: string | null;
  command?: string | null;
}

export interface ModelCallEvent extends StreamBase {
  object: 'model.call';
  model_name: string;
}

export interface ExecutionTiming {
  kind: 'model' | 'tool' | 'segment';
  startedAt: number; // Epoch seconds, using server event timestamps only.
  endedAt: number;
}

/** Content-free replay records; sequence_number is the SSE cursor, not BizEvent.seq. */
export interface TimingEvent extends StreamBase {
  object: string;
  id?: string;
  type?: string;
  role?: string | null;
  source_id?: string | null;
  call_id?: string;
  model_name?: string;
}

export type ConsoleStreamEvent =
  | McpConnectEvent
  | ModelCallEvent
  | (StreamBase & { object: 'model.call.end'; input_tokens: number; output_tokens: number })
  | (StreamBase & {
      object: 'tool.result.start' | 'tool.result.end';
      tool_name: string;
      call_id: string;
    })
  | (StreamBase & ContextUsage & { object: 'context.usage' })
  | (StreamBase & {
      object: 'response';
      id: string;
      status: ResponseStatus;
      usage: Record<string, unknown> | null;
      error: ChatError | null;
    })
  | (StreamBase & TraceMessage)
  | (StreamBase & MessageContent & { object: 'content'; msg_id: string })
  | (StreamBase & ApiErrorBody & { object: 'error' })
  | (StreamBase & { object: 'segment'; segment: Segment })
  | (StreamBase & { object: 'biz_event'; biz_event: BizEvent })
  | (StreamBase & { object: 'artifact.registered'; artifact: Artifact })
  | (StreamBase & { object: 'followup.generated'; followup: FollowUp });

export interface SessionSnapshot {
  has_more?: boolean; // Older servers return all chats without pagination metadata.
  session: ConsoleSession;
  chats: ConsoleChat[];
}

export interface TraceResponse {
  timing_events?: TimingEvent[];
  mcp_connections?: McpConnectEvent[];
  messages: TraceMessage[];
  biz_events: BizEvent[];
}
