import { SUPPORTED_DATA_SOURCE_TYPES } from "../helpers/types";

/** Return locally supported data source type metadata for the add form. */
export function useDataSourceTypes() {
  return {
    types: SUPPORTED_DATA_SOURCE_TYPES,
    loading: false,
    reload: () => {},
  };
}
