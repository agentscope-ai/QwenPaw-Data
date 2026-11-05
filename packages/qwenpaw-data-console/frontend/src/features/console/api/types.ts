export type SessionStatus = 'idle' | 'running';
export type ChatStatus =
  | 'created'
  | 'running'
  | 'completed'
  | 'failed'
  | 'canceled';
export type ResponseStatus =
  | 'created'
  | 'in_progress'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface ApiErrorBody {
  code: 'UNAUTHORIZED' | 'FORBIDDEN' | 'NOT_FOUND' | 'CONFLICT' | 'VALIDATION';
  message: string;
  details: Record<string, unknown> | null;
}

export interface AttachmentRef {
  attachment_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
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

export interface RuntimeSettings {
  react_max_iters: number;
  llm_retry_enabled: boolean;
  llm_max_retries: number;
}

export interface ConsoleSession {
  id: string;
  agent_id: string;
  title: string;
  status: SessionStatus;
  datasource_id: string | null;
  chat_count: number;
  channel: string;
  created_at: string;
  updated_at: string;
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

export type ArtifactAccessPurpose = 'preview' | 'download';

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

export interface Segment {
  segment_id: string;
  chat_id: string | null;
  title: string;
  input: string | null;
  behavior: string;
  conclusion: string;
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
  id: string;
  session_id: string;
  sequence: number;
  user_input: string;
  datasource_id: string | null;
  kind: 'simple' | 'planned';
  status: ChatStatus;
  last_sequence_number: number;
  started_at: string | null;
  completed_at: string | null;
  active_duration_ms: number;
  error: { code: string; message: string } | null;
  plan: Plan | null;
  segments: Segment[];
  biz_events: BizEvent[];
  artifacts: Artifact[];
  followup: FollowUp | null;
}

interface StreamBase {
  sequence_number: number;
  session_id: string;
  chat_id: string;
}

export type ConsoleStreamEvent =
  | (StreamBase & {
      object: 'response';
      id: string;
      status: ResponseStatus;
      usage: Record<string, unknown> | null;
      error: ApiErrorBody | null;
    })
  | (StreamBase & TraceMessage)
  | (StreamBase & MessageContent & { object: 'content'; msg_id: string })
  | (StreamBase & ApiErrorBody & { object: 'error' })
  | (StreamBase & { object: 'segment'; segment: Segment })
  | (StreamBase & { object: 'biz_event'; biz_event: BizEvent })
  | (StreamBase & { object: 'artifact.registered'; artifact: Artifact })
  | (StreamBase & { object: 'followup.generated'; followup: FollowUp });

export interface SessionSnapshot {
  session: ConsoleSession;
  chats: ConsoleChat[];
}

export interface TraceResponse {
  messages: TraceMessage[];
  biz_events: BizEvent[];
}
