export type WorkspaceTab = 'turns' | 'artifacts' | 'settlement' | 'trace';

export interface ConversationTurn {
  id: string;
  number: string;
  title: string;
  dataSource: string;
  sentAt: string;
  status: 'done' | 'running' | 'awaiting';
}

export interface Artifact {
  id: string;
  name: string;
  title: string;
  type: string;
  meta: string;
  segments?: Array<{
    id: string;
    number?: number;
    title: string;
  }>;
  mimeType?: string;
  sizeBytes?: number;
  path?: string;
  access?: {
    relativePath: string;
  };
}
