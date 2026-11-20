import type { SemanticYesNo } from './semanticConfig';

export interface ColumnCreateRequest {
  datasource_id: string;
  domain_id: number;
  dataset_id: number;
  field_name: string;
  description: string;
  data_type: string;
  is_primary: SemanticYesNo;
  is_nullable: SemanticYesNo;
}

export type ColumnUpdateRequest = Pick<
  ColumnCreateRequest,
  'field_name' | 'description' | 'data_type' | 'is_primary' | 'is_nullable'
>;

export interface ColumnResponse extends ColumnCreateRequest {
  id: number;
  dataset_name: string;
  datasource_name: string;
  domain_name: string;
}

export type ColumnItem = ColumnResponse;

export interface QueryColumnParams {
  datasource_id?: string;
  domain_id?: number;
  dataset_id?: number;
  page?: number;
  size?: number;
}

export type ColumnUpsertInput = ColumnCreateRequest & { id?: number };
