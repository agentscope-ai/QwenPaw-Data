import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import { queryDataSourceMetadata } from '@/services/dataSource';
import type { DataSourceItem } from '@/types/dataSource';
import { normalizeListResponse } from '@/utils/listResponse';
import { formatDatasourceLabel } from '@/utils';

const normalizeList = normalizeListResponse<DataSourceItem>;

// 数据源选项（轻量，用于下拉）
export interface DataSourceOption {
  label: string;
  value: string;
  datasource_id: string;
  datasource_type?: string;
}

interface DataSourceOptionsState {
  list: DataSourceItem[];
  options: DataSourceOption[];
  simpleOptions: DataSourceOption[];
  loading: boolean;
  fetched: boolean;
  fetchOptions: (force?: boolean) => Promise<void>;
  reset: () => void;
}

const initialState = {
  list: [] as DataSourceItem[],
  options: [] as DataSourceOption[],
  simpleOptions: [] as DataSourceOption[],
  loading: false,
  fetched: false,
};

// 创建 store（带 devtools 中间件便于调试）
export const useDataSourceOptionsStore = create<DataSourceOptionsState>()(
  devtools(
    (set) => ({
      ...initialState,

      // 获取数据源列表
      fetchOptions: async (force = false) => {
        const state = useDataSourceOptionsStore.getState();
        // 防止重复请求：如果正在加载或已获取过数据（非强制刷新），则跳过
        if (state.loading || (!force && state.fetched)) {
          return;
        }
        set({ loading: true }, false, 'fetchOptions/start');
        try {
          const result = await queryDataSourceMetadata({});
          const list = normalizeList(result);
          set({
            list,
            options: list.map((item) => ({
              label: formatDatasourceLabel(item.datasource_name, item.datasource_id),
              value: item.datasource_id,
              datasource_id: item.datasource_id,
              datasource_type: item.datasource_type,
            })),
            simpleOptions: list.map((item) => ({
              label: formatDatasourceLabel(item.datasource_name, item.datasource_id),
              value: item.datasource_id,
              datasource_id: item.datasource_id,
              datasource_type: item.datasource_type,
            })),
            loading: false,
            fetched: true,
          }, false, '数据源选项请求成功');
        } catch {
          set({ loading: false }, false, 'fetchOptions/error');
        }
      },

      // 重置所有状态
      reset: () => {
        set(initialState, false, 'reset');
      },
    }),
    { name: 'DataSourceOptionsStore' }
  )
);

// 导出 selector hooks 便于组件使用
export const useDataSourceOptions = () => useDataSourceOptionsStore((state) => state.options);
export const useDataSourceSimpleOptions = () => useDataSourceOptionsStore((state) => state.simpleOptions);
export const useDataSourceList = () => useDataSourceOptionsStore((state) => state.list);
export const useDataSourceOptionsLoading = () => useDataSourceOptionsStore((state) => state.loading);
