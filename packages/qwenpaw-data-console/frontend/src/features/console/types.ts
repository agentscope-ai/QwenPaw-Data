export type WorkspaceTab = 'turns' | 'artifacts' | 'settlement' | 'trace';

export interface ConversationTurn {
  id: string;
  number: string;
  title: string;
  summary: string;
  dataSource: string;
  sentAt: string;
  status: 'done' | 'running' | 'awaiting';
  duration?: string;
}

export interface PlanStep {
  id: string;
  title: string;
  status: 'done' | 'running' | 'todo';
}

export interface Artifact {
  id: string;
  name: string;
  title: string;
  type: string;
  meta: string;
  important?: boolean;
  mimeType?: string;
  sizeBytes?: number;
  createdAt?: string;
  path?: string;
  access?: {
    relativePath: string;
  };
}

export interface TraceStep {
  id: string;
  title: string;
  detail: string;
  tool: string;
  status: 'done' | 'running' | 'error';
}
