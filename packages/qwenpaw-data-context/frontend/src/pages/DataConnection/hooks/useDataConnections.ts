import { useCallback, useState } from "react";
import {
  dataSourceApi,
  httpDataSourceApi,
  type DataSourceRecord,
} from "@/services/dataConnection";

/** Load, refresh, and remove Data Connection records for the list page. */
export function useDataConnections() {
  const [connections, setConnections] = useState<DataSourceRecord[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await httpDataSourceApi.list();
      setConnections(res.items ?? []);
    } catch (error) {
      console.error("Failed to load data sources:", error);
      setConnections([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const removeConnection = useCallback(async (id: string) => {
    // DELETE /{id} returns 204 with no body on success.
    await httpDataSourceApi.remove(id);

    const res = await dataSourceApi.list();
    setConnections(res.items ?? []);
  }, []);

  return {
    connections,
    loading,
    refresh,
    removeConnection,
  };
}
