import { useEffect } from 'react';
import { create } from 'zustand';
import { devtools } from 'zustand/middleware';
import { queryBusinessDomainList } from '@/services/businessDomain';
import { type SelectOption } from '@/utils/listResponse';
import { useAuthStore } from './authStore';

interface BusinessDomainOptionsState {
  /** 按租户和工作空间缓存全部业务域，不按数据源分桶。 */
  optionsMap: Record<string, SelectOption[]>;
  /** 正在加载的工作空间。 */
  loadingIds: Set<string>;
  /** 已加载的工作空间。 */
  fetchedIds: Set<string>;
  /** 保留旧调用参数兼容数据集页面，数据源不再限制业务域。 */
  fetchOptions: (datasourceId?: string, force?: boolean) => Promise<SelectOption[]>;
  /** 业务域变更后失效缓存。 */
  invalidate: (datasourceId?: string) => void;
  /** 重置缓存。 */
  reset: () => void;
}

const initialState = {
  optionsMap: {} as Record<string, SelectOption[]>,
  loadingIds: new Set<string>(),
  fetchedIds: new Set<string>(),
};
const scopeKey = (state: { tenantId: string; activeWorkspaceId: string }) =>
  JSON.stringify([state.tenantId, state.activeWorkspaceId]);
const pending = new Map<string, Promise<SelectOption[]>>();
let generation = 0;

export const useBusinessDomainOptionsStore = create<BusinessDomainOptionsState>()(
  devtools(
    (set, get) => ({
      ...initialState,
      fetchOptions: async (_datasourceId, force = false) => {
        const key = scopeKey(useAuthStore.getState());
        const existing = pending.get(key);
        if (existing) return existing;
        if (!force && get().fetchedIds.has(key)) return get().optionsMap[key] ?? [];
        const requestGeneration = generation;
        set((prev) => ({ loadingIds: new Set([...prev.loadingIds, key]) }));
        const request = (async () => {
          try {
            const options: SelectOption[] = [];
            let page = 1;
            let total = 0;
            do {
              const result = await queryBusinessDomainList({ page, size: 500 });
              total = result.total;
              options.push(
                ...result.records.map((item) => ({
                  label: item.domain_name,
                  value: item.domain_id,
                })),
              );
              if (!result.records.length) break;
              page += 1;
            } while (options.length < total);
            if (requestGeneration === generation) {
              set((prev) => ({
                optionsMap: { ...prev.optionsMap, [key]: options },
                fetchedIds: new Set([...prev.fetchedIds, key]),
              }));
            }
            return options;
          } catch {
            return [];
          } finally {
            if (requestGeneration === generation) {
              pending.delete(key);
              set((prev) => ({
                loadingIds: new Set([...prev.loadingIds].filter((id) => id !== key)),
              }));
            }
          }
        })();
        pending.set(key, request);
        return request;
      },
      invalidate: () => {
        generation += 1;
        pending.clear();
        set({ ...initialState });
      },
      reset: () => get().invalidate(),
    }),
    { name: 'BusinessDomainOptionsStore' },
  ),
);

const EMPTY_OPTIONS: SelectOption[] = [];

/** 获取当前工作空间全部业务域；旧数据源参数不再参与过滤。 */
export const useBusinessDomainOptions = (_datasourceId?: string) => {
  void _datasourceId;
  const key = useAuthStore(scopeKey);
  const fetched = useBusinessDomainOptionsStore((state) => state.fetchedIds.has(key));
  useEffect(() => {
    if (!fetched) void useBusinessDomainOptionsStore.getState().fetchOptions();
  }, [key, fetched]);
  return useBusinessDomainOptionsStore((state) => state.optionsMap[key] ?? EMPTY_OPTIONS);
};

/** 当前工作空间域选项加载状态。 */
export const useBusinessDomainOptionsLoading = (_datasourceId?: string) => {
  void _datasourceId;
  const key = useAuthStore(scopeKey);
  return useBusinessDomainOptionsStore((state) => state.loadingIds.has(key));
};
