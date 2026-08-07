/**
 * Zustand Store 统一出口
 *
 * 使用方式:
 * import { useDataSourceOptionsStore, useBusinessDomainOptionsStore } from '@/store';
 */

// DataSourceOptions Store
export {
  useDataSourceOptionsStore,
  useDataSourceOptions,
  useDataSourceSimpleOptions,
  useDataSourceList,
  useDataSourceOptionsLoading,
} from './dataSourceOptions';

// BusinessDomainOptions Store
export {
  useBusinessDomainOptionsStore,
  useBusinessDomainOptions,
  useBusinessDomainOptionsLoading,
} from './businessDomainOptions';
