import type {
  BusinessDomainCreateRequest,
  BusinessDomainQueryParams,
  BusinessDomainResponse,
  BusinessDomainUpdateRequest,
} from '@/types/businessDomain';
import type { SemanticConfigDeleteResponse, SemanticConfigPage } from '@/types/semanticConfig';
import { assertSemanticConfigPage, pickSemanticFields } from '@/utils/semanticConfigContract';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

const BUSINESS_DOMAIN_API = semanticConfigApi('/biz-domain');

export const queryBusinessDomainList = async (
  params: BusinessDomainQueryParams = {},
): Promise<SemanticConfigPage<BusinessDomainResponse>> => {
  const response = await request.get<unknown>(BUSINESS_DOMAIN_API, {
    params: {
      ...pickSemanticFields(params, ['domain_name'] as const),
      page: params.page ?? 1,
      size: params.size ?? 20,
    },
  });
  return assertSemanticConfigPage<BusinessDomainResponse>(response);
};

export const createBusinessDomain = (
  data: BusinessDomainCreateRequest,
): Promise<BusinessDomainResponse> => {
  return request.post(
    BUSINESS_DOMAIN_API,
    pickSemanticFields(data, ['domain_name', 'description'] as const),
  );
};

export const updateBusinessDomain = (
  id: number,
  data: BusinessDomainUpdateRequest,
): Promise<BusinessDomainResponse> => {
  return request.put(
    `${BUSINESS_DOMAIN_API}/${id}`,
    pickSemanticFields(data, ['domain_name', 'description'] as const),
  );
};

export const deleteBusinessDomain = (id: number): Promise<SemanticConfigDeleteResponse> => {
  return request.delete(`${BUSINESS_DOMAIN_API}/${id}`);
};
