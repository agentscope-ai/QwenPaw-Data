import { useCallback, useEffect, useState } from 'react';
import { queryDimensionList } from '@/services/dimensionManagement';
import { queryDatasetMeta } from '@/services/datasetManagement';
import type { DimensionItem } from '@/types/dimensionManagement';
import { normalizeListResponse, type SelectOption } from '@/utils/listResponse';
import {
  useDataSourceOptionsStore,
  useDataSourceOptions,
  useDataSourceSimpleOptions,
  useDataSourceOptionsLoading,
  useBusinessDomainOptionsStore,
  useBusinessDomainOptions,
} from '@/store';

const dimensionNameCache = new Map<number, SelectOption<string>[]>();
const datasetCache = new Map<number, SelectOption[]>();

/** 加载维度名称下拉（按业务域缓存） */
export async function loadDimensionNameOptions(domainId: number): Promise<SelectOption<string>[]> {
  if (!domainId) return [];
  const cached = dimensionNameCache.get(domainId);
  if (cached) return cached;

  const res = await queryDimensionList({ domain_id: domainId, page: 1, size: 200 });
  const list = normalizeListResponse<DimensionItem>(res);
  const names = [...new Set(list.map((item) => item.dimension_name).filter(Boolean))] as string[];
  const options = names.map((name) => ({ label: name, value: name }));
  dimensionNameCache.set(domainId, options);
  return options;
}

/** 加载维度 ID 下拉 */
export async function loadDimensionIdOptions(domainId: number): Promise<SelectOption[]> {
  if (!domainId) return [];
  const res = await queryDimensionList({ domain_id: domainId, page: 1, size: 500 });
  return normalizeListResponse<DimensionItem>(res)
    .filter((item) => item.id && item.dimension_name)
    .map((item) => ({ label: item.dimension_name, value: item.id }));
}

/** 加载数据集下拉（按业务域缓存） */
export async function loadDatasetOptions(domainId: number): Promise<SelectOption[]> {
  if (!domainId) return [];
  const cached = datasetCache.get(domainId);
  if (cached) return cached;

  const res = await queryDatasetMeta({ domain_id: domainId, page: 1, size: 500 });
  const options = normalizeListResponse<{ dataset_id?: number; id?: number; dataset_name: string }>(res).map(
    (item) => ({
      label: item.dataset_name,
      value: item.dataset_id ?? item.id!,
    })
  );
  datasetCache.set(domainId, options);
  return options;
}

export function invalidateDimensionCache(domainId?: number) {
  if (domainId) dimensionNameCache.delete(domainId);
  else dimensionNameCache.clear();
}

export function invalidateDatasetCache(domainId?: number) {
  if (domainId) datasetCache.delete(domainId);
  else datasetCache.clear();
}

type DataSourceLabelStyle = 'simple' | 'full';

/** 数据源下拉：自动拉取全局缓存 */
export function useDataSourceFilterOptions(labelStyle: DataSourceLabelStyle = 'simple') {
  useEffect(() => {
    // 强制刷新：每次页面初始化都重新请求数据源数据
    useDataSourceOptionsStore.getState().fetchOptions(true);
  }, []);

  const fullOptions = useDataSourceOptions();
  const simpleOptions = useDataSourceSimpleOptions();
  const loading = useDataSourceOptionsLoading();

  return {
    options: labelStyle === 'full' ? fullOptions : simpleOptions,
    loading,
    refresh: () => useDataSourceOptionsStore.getState().fetchOptions(true),
  };
}

/** 级联筛选：业务域走 store 缓存，维度/数据集走模块缓存 */
export function useCascadeFilterOptions() {
  const loadDomains = useCallback(
    (datasourceId: string) => useBusinessDomainOptionsStore.getState().fetchOptions(datasourceId),
    []
  );

  return {
    loadDomains,
    loadDimensionNames: loadDimensionNameOptions,
    loadDimensionIds: loadDimensionIdOptions,
    loadDatasets: loadDatasetOptions,
    invalidateDomainCache: (datasourceId?: string) =>
      useBusinessDomainOptionsStore.getState().invalidate(datasourceId),
    invalidateDimensionCache,
    invalidateDatasetCache,
  };
}

/** 列表筛选：数据源 + 业务域级联（业务域选项来自 store 缓存） */
export function useDatasourceDomainFilter(labelStyle: DataSourceLabelStyle = 'full') {
  const { options: dataSourceOptions } = useDataSourceFilterOptions(labelStyle);
  const { loadDomains } = useCascadeFilterOptions();
  const [searchDatasourceId, setSearchDatasourceId] = useState('');
  const domainOptions = useBusinessDomainOptions(searchDatasourceId);

  const selectDatasource = useCallback(
    async (dsId: string) => {
      setSearchDatasourceId(dsId || '');
      if (dsId) await loadDomains(dsId);
    },
    [loadDomains],
  );

  return {
    dataSourceOptions,
    domainOptions,
    searchDatasourceId,
    setSearchDatasourceId,
    selectDatasource,
    loadDomains,
  };
}
