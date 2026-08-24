import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import { queryBusinessDomainList } from '@/services/businessDomain';
import { normalizeListResponse, type SelectOption } from '@/utils/listResponse';

interface BusinessDomainOptionsState {
  /** 按 datasourceId 分组的业务域下拉选项 */
  optionsMap: Record<string, SelectOption[]>;
  /** 正在加载的 datasourceId 集合 */
  loadingIds: Set<string>;
  /** 已获取过的 datasourceId 集合 */
  fetchedIds: Set<string>;
  /** 按 datasourceId 获取业务域选项（带缓存） */
  fetchOptions: (datasourceId: string, force?: boolean) => Promise<SelectOption[]>;
  /** 强制失效指定数据源的业务域缓存（用于增删改后刷新） */
  invalidate: (datasourceId?: string) => void;
  /** 重置所有状态 */
  reset: () => void;
}

const initialState = {
  optionsMap: {} as Record<string, SelectOption[]>,
  loadingIds: new Set<string>(),
  fetchedIds: new Set<string>(),
};

export const useBusinessDomainOptionsStore = create<BusinessDomainOptionsState>()(
  devtools(
    (set, get) => ({
      ...initialState,

      fetchOptions: async (datasourceId: string, force = false) => {
        if (!datasourceId) return [];
        const state = get();
        // 防止重复请求
        if (state.loadingIds.has(datasourceId) || (!force && state.fetchedIds.has(datasourceId))) {
          return state.optionsMap[datasourceId] ?? [];
        }

        set(
          (prev) => ({
            loadingIds: new Set([...prev.loadingIds, datasourceId]),
          }),
          false,
          'fetchOptions/start',
        );

        try {
          const result = await queryBusinessDomainList({ datasource_id: datasourceId, page: 1, size: 500 });
          const list = normalizeListResponse<{ domain_id: number; domain_name: string }>(result);
          const options: SelectOption[] = list.map((item) => ({
            label: item.domain_name,
            value: item.domain_id,
          }));

          set(
            (prev) => ({
              optionsMap: { ...prev.optionsMap, [datasourceId]: options },
              loadingIds: new Set([...prev.loadingIds].filter((id) => id !== datasourceId)),
              fetchedIds: new Set([...prev.fetchedIds, datasourceId]),
            }),
            false,
            '业务域选项请求成功',
          );

          return options;
        } catch {
          set(
            (prev) => ({
              loadingIds: new Set([...prev.loadingIds].filter((id) => id !== datasourceId)),
            }),
            false,
            'fetchOptions/error',
          );
          return [];
        }
      },

      invalidate: (datasourceId?: string) => {
        if (datasourceId) {
          set(
            (prev) => ({
              optionsMap: { ...prev.optionsMap, [datasourceId]: [] },
              fetchedIds: new Set([...prev.fetchedIds].filter((id) => id !== datasourceId)),
            }),
            false,
            'invalidate/single',
          );
        } else {
          set(
            {
              optionsMap: {},
              fetchedIds: new Set(),
            },
            false,
            'invalidate/all',
          );
        }
      },

      reset: () => {
        set(initialState, false, 'reset');
      },
    }),
    { name: 'BusinessDomainOptionsStore' },
  ),
);

const EMPTY_OPTIONS: SelectOption[] = [];

/** 获取指定数据源下的业务域选项 */
export const useBusinessDomainOptions = (datasourceId: string) =>
  useBusinessDomainOptionsStore((state) => state.optionsMap[datasourceId] ?? EMPTY_OPTIONS);

/** 获取指定数据源下的业务域加载状态 */
export const useBusinessDomainOptionsLoading = (datasourceId: string) =>
  useBusinessDomainOptionsStore((state) => state.loadingIds.has(datasourceId));
