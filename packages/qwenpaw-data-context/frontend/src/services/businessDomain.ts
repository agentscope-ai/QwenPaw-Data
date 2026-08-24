import { BusinessDomainQueryParams, BusinessDomainItem } from '@/types/businessDomain';
import { request } from '@/utils/request';
import { semanticConfigApi } from './api';

// 查询业务域列表（分页）
export const queryBusinessDomainList = (params: BusinessDomainQueryParams) => {
  return request.get(semanticConfigApi('/biz-domain'), { params });
};

// 新增业务域
export const createBusinessDomain = (data: Partial<BusinessDomainItem>) => {
  return request.post(semanticConfigApi('/biz-domain'), data);
};

// 编辑业务域
export const updateBusinessDomain = (id: number, data: Partial<BusinessDomainItem>) => {
  return request.put(semanticConfigApi(`/biz-domain/${id}`), data);
};

// 删除业务域
export const deleteBusinessDomain = (id: string | number) => {
  return request.delete(semanticConfigApi(`/biz-domain/${id}`));
};
