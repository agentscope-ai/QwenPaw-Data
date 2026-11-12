import { ApiRequestError } from './apiError';
import { request, requestBlob } from './request';

export interface KgDocsApiEnvelope<T> {
  code: number;
  message: string;
  data: T | null;
}

export type IngestStatus = 'building' | 'ready' | 'failed';

interface KgDocumentBase {
  doc_id: string;
  filename: string;
  file_size: number;
  download_url: string;
  ingest_status: IngestStatus;
  last_modified: string | null;
}

export interface KgDocument extends KgDocumentBase {
  ingest_error: string | null;
  owner_id: string;
  owner_name: string;
  can_delete: boolean;
  retry_action: 'reingest' | 'delete' | null;
}

export type KgUploadedDocument = KgDocumentBase;

export interface KgDocsListData {
  list: KgDocument[];
  page: number;
  page_size: number;
  total: number;
  status_counts?: Record<IngestStatus, number>;
}

export interface KgDocsDeleteData {
  doc_id: string;
}

export type KgDocPreviewKind = 'pdf' | 'text' | 'markdown' | 'docx';

export interface KgDocPreview {
  doc_id: string;
  filename: string;
  kind: KgDocPreviewKind;
  text: string | null;
  truncated: boolean;
}

function ensureSuccess<T>(envelope: KgDocsApiEnvelope<T>): T {
  if (envelope.code !== 0 || envelope.data == null) {
    throw new ApiRequestError(envelope.message || 'Request failed', {
      status: 200,
      code: String(envelope.code),
      body: envelope,
    });
  }
  return envelope.data;
}

export const kgDocsApi = {
  listKgDocs: async (params: {
    page: number;
    pageSize: number;
    status?: IngestStatus;
    keyword?: string;
  }) => {
    const query = new URLSearchParams({
      page: String(params.page),
      page_size: String(params.pageSize),
    });
    if (params.status) query.set('status', params.status);
    if (params.keyword) query.set('keyword', params.keyword);
    const envelope = await request<KgDocsApiEnvelope<KgDocsListData>>(
      `/v1/docs?${query.toString()}`,
      { redirectOnForbidden: false },
    );
    return ensureSuccess(envelope);
  },

  uploadKgDoc: async (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    const envelope = await request<KgDocsApiEnvelope<KgUploadedDocument>>('/v1/docs/upload', {
      method: 'POST',
      body: formData,
      redirectOnForbidden: false,
    });
    return ensureSuccess(envelope);
  },

  reingestKgDoc: async (docId: string) => {
    const query = new URLSearchParams({ doc_id: docId });
    const envelope = await request<KgDocsApiEnvelope<KgUploadedDocument>>(
      `/v1/docs/reingest?${query.toString()}`,
      {
        method: 'POST',
        redirectOnForbidden: false,
      },
    );
    return ensureSuccess(envelope);
  },

  deleteKgDoc: async (docId: string) => {
    const envelope = await request<KgDocsApiEnvelope<KgDocsDeleteData>>(
      `/v1/docs/${encodeURIComponent(docId)}`,
      {
        method: 'DELETE',
        redirectOnForbidden: false,
      },
    );
    return ensureSuccess(envelope);
  },

  previewKgDoc: async (docId: string) => {
    const query = new URLSearchParams({ doc_id: docId });
    const envelope = await request<KgDocsApiEnvelope<KgDocPreview>>(
      `/v1/docs/preview?${query.toString()}`,
      { redirectOnForbidden: false },
    );
    return ensureSuccess(envelope);
  },

  downloadKgDocBlob: async (docId: string) => {
    return requestBlob(`/v1/docs/${encodeURIComponent(docId)}/download`, {
      redirectOnForbidden: false,
    });
  },
};
