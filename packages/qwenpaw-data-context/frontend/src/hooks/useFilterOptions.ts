import { useCallback, useEffect, useState } from 'react';
import { useAuthStore } from '@/store/authStore';
import { setActiveDataSource } from '@/services/dataSource';
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

const dimensionNameCache = new Map<string, SelectOption<string>[]>();
const datasetCache = new Map<string, SelectOption[]>();

const optionCacheKey = (domainId: number, datasourceId?: string) => {
  const { tenantId, activeWorkspaceId } = useAuthStore.getState();
  return JSON.stringify([tenantId, activeWorkspaceId, domainId, datasourceId]);
};

/** 加载维度名称下拉（按业务域缓存） */
export async function loadDimensionNameOptions(domainId: number): Promise<SelectOption<string>[]> {
  if (!domainId) return [];
  const key = optionCacheKey(domainId);
  const cached = dimensionNameCache.get(key);
  if (cached) return cached;

  const res = await queryDimensionList({ domain_id: domainId, page: 1, size: 200 });
  const list = normalizeListResponse<DimensionItem>(res);
  const names = [...new Set(list.map((item) => item.dimension_name).filter(Boolean))] as string[];
  const options = names.map((name) => ({ label: name, value: name }));
  dimensionNameCache.set(key, options);
  return options;
}

/** 加载维度 ID 下拉 */
export async function loadDimensionIdOptions(domainId: number): Promise<SelectOption[]> {
  if (!domainId) return [];
  const res = await queryDimensionList({ domain_id: domainId, page: 1, size: 500 });
  return normalizeListResponse<DimensionItem>(res)
    .filter((item) => item.dimension_id && item.dimension_name)
    .map((item) => ({ label: item.dimension_name, value: item.dimension_id }));
}

/** 加载数据集下拉（按业务域缓存） */
export async function loadDatasetOptions(
  domainId: number,
  datasourceId?: string,
): Promise<SelectOption[]> {
  if (!domainId) return [];
  const key = optionCacheKey(domainId, datasourceId);
  const cached = datasetCache.get(key);
  if (cached) return cached;

  const res = await queryDatasetMeta({
    domain_id: domainId,
    datasource_id: datasourceId,
    page: 1,
    size: 500,
  });
  const options = normalizeListResponse<{ dataset_id: number; dataset_name: string }>(res).map(
    (item) => ({
      label: item.dataset_name,
      value: item.dataset_id,
    }),
  );
  datasetCache.set(key, options);
  return options;
}

export function invalidateDimensionCache(domainId?: number) {
  void domainId;
  dimensionNameCache.clear();
}

export function invalidateDatasetCache(domainId?: number) {
  void domainId;
  datasetCache.clear();
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
    (datasourceId?: string) => useBusinessDomainOptionsStore.getState().fetchOptions(datasourceId),
    [],
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

/** 列表筛选：数据源保留物理归属，业务域独立加载工作空间全量。 */
export function useDatasourceDomainFilter(labelStyle: DataSourceLabelStyle = 'full') {
  const { options: dataSourceOptions } = useDataSourceFilterOptions(labelStyle);
  const { loadDomains } = useCascadeFilterOptions();
  const [searchDatasourceId, setSearchDatasourceId] = useState('');
  const domainOptions = useBusinessDomainOptions(searchDatasourceId);

  const selectDatasource = useCallback(
    async (dsId: string) => {
      setSearchDatasourceId(dsId || '');
      if (dsId) {
        void setActiveDataSource(dsId).catch(() => undefined);
        await loadDomains(dsId);
      }
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
