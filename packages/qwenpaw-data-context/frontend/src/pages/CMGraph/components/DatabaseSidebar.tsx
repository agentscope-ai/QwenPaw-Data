import { useEffect, useState, useCallback, useRef } from "react";
import { Select, Tag, Input, Spin } from "@/design";
import { SearchOutlined } from "@/design";
import { useTranslation } from "react-i18next";

import { explorerApi, mgApi } from "@/services/cmgraph";
import { apiNodeToGraphNode } from "@/services/cmgraph";
import type {
  SchemaNodeLabel,
  SchemaRelType,
  ApiGraphNode,
  ApiGraphEdge,
  Domain,
  Zone,
  MatchMode,
} from "@/services/cmgraph";
import { NODE_TYPE_COLORS } from "../mock-data";
import styles from "../index.module.less";

type SidebarTab = "database" | "search";

const DOMAIN_PAGE_SIZE = 500;

async function fetchAllDomains(): Promise<Domain[]> {
  const firstPage = await mgApi.listDomains({ page: 1, page_size: DOMAIN_PAGE_SIZE });
  const meta = firstPage.meta;
  if (!meta?.has_more) return firstPage.data ?? [];

  const totalPages = Math.ceil(meta.total / meta.page_size);
  const remainingPages = await Promise.all(
    Array.from({ length: totalPages - 1 }, (_, index) =>
      mgApi.listDomains({ page: index + 2, page_size: meta.page_size }),
    ),
  );
  return [
    ...(firstPage.data ?? []),
    ...remainingPages.flatMap(({ data }) => data ?? []),
  ];
}

/** Subgraph search result passed to the parent for rendering. */
export interface SubgraphSearchResult {
  query: string;
  hitNodes: ApiGraphNode[];
  nodes: ApiGraphNode[];
  edges: ApiGraphEdge[];
}

interface DatabaseSidebarProps {
  /** Whether to show the database selector for the current graph zone. */
  showDatabaseSelector: boolean;
  /** Called when a node-label tag is clicked. */
  onLabelClick?: (label: string, zone?: string) => void;
  /** Called when a relationship-type tag is clicked. */
  onRelTypeClick?: (relType: string, zone?: string) => void;
  /** Called with the datasource code selected for TG/MG requests. */
  onDatasourceCodeChange?: (datasourceCode?: string) => void;
  /** Called when a subgraph search completes, passing the full subgraph data. */
  onSubgraphSearch?: (result: SubgraphSearchResult) => void;
}

export default function DatabaseSidebar({
  showDatabaseSelector,
  onLabelClick,
  onRelTypeClick,
  onDatasourceCodeChange,
  onSubgraphSearch,
}: DatabaseSidebarProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<SidebarTab>("database");

  // Database tab state
  const [selectedDatasourceCode, setSelectedDatasourceCode] = useState<string>();
  const [databaseOptions, setDatabaseOptions] = useState<
    { label: string; value: string }[]
  >([]);
  const [nodeLabels, setNodeLabels] = useState<{ label: string; count: number; color: string; zone?: string }[]>([]);
  const [relationshipTypes, setRelationshipTypes] = useState<{ type: string; count: number; zone?: string }[]>([]);
  // Schema loading state — only affects the database tab's label/rel sections,
  // not the entire sidebar.
  const [schemaLoading, setSchemaLoading] = useState(true);

  // Search tab state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<ApiGraphNode[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchStats, setSearchStats] = useState<string | null>(null);

  // Search configuration
  const [searchScope, setSearchScope] = useState<Zone[]>(["metadata", "trace", "knowledge"]);
  const [matchMode, setMatchMode] = useState<MatchMode>("fuzzy");
  const [hops, setHops] = useState(1);

  // Error states
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Unmount protection ref
  const isMountedRef = useRef(true);
  useEffect(() => {
    isMountedRef.current = true;
    return () => { isMountedRef.current = false; };
  }, []);

  // MG stores the datasource_id in its legacy datasource_id property.
  useEffect(() => {
    let cancelled = false;
    fetchAllDomains()
      .then((domains) => {
        if (cancelled) return;
        // Extract unique datasource codes as database options.
        const seen = new Set<string>();
        const options: { label: string; value: string }[] = [];
        for (const domain of domains) {
          const dsId = domain.datasource_id;
          if (dsId && !seen.has(dsId)) {
            seen.add(dsId);
            options.push({ label: dsId, value: dsId });
          }
        }
        setDatabaseOptions(options);
        const firstDatasourceCode = options[0]?.value;
        setSelectedDatasourceCode(firstDatasourceCode);
        onDatasourceCodeChange?.(firstDatasourceCode);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Failed to load database list:", err);
      });
    return () => { cancelled = true; };
  }, [onDatasourceCodeChange]);

  // Schema is global: the endpoint does not accept a database parameter.
  useEffect(() => {
    let cancelled = false;
    setSchemaLoading(true);
    setSchemaError(null);
    explorerApi
      .getSchema()
      .then((schema) => {
        if (cancelled) return;
        const totalNodeCount = schema.node_labels.reduce((sum, n) => sum + n.count, 0);
        const mappedLabels: {
          label: string;
          count: number;
          color: string;
          zone?: string;
        }[] = schema.node_labels.map((item: SchemaNodeLabel) => ({
            label: item.label,
            count: item.count,
            color: NODE_TYPE_COLORS[item.label] || "#78909c",
            zone: item.zone,
          }));
        if (mappedLabels.length > 0) {
          mappedLabels.unshift({
            label: "*",
            count: totalNodeCount,
            color: NODE_TYPE_COLORS["*"] || "#78909c",
            zone: undefined,
          });
        }

        const totalRelCount = schema.relationship_types.reduce((sum, r) => sum + r.count, 0);
        const mappedRels: { type: string; count: number; zone?: string }[] =
          schema.relationship_types.map((item: SchemaRelType) => ({
            type: item.type,
            count: item.count,
            zone: item.zone === "cross" ? item.source_zone : item.zone,
          }));
        if (mappedRels.length > 0) {
          mappedRels.unshift({ type: "*", count: totalRelCount, zone: undefined });
        }

        setNodeLabels(mappedLabels);
        setRelationshipTypes(mappedRels);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Failed to fetch schema:", err);
        setSchemaError(t("kgBrowser.schemaLoadFailed"));
      })
      .finally(() => {
        if (cancelled) return;
        setSchemaLoading(false);
      });
    return () => { cancelled = true; };
  }, [t]);

  // Subgraph search logic — uses searchSubgraph API for n-hop expansion.
  // Accepts optional overrides so config button clicks can trigger a search
  // with the newly selected value before state has updated.
  const handleSearch = useCallback(async (
    query: string,
    overrides?: { scope?: Zone[]; mode?: MatchMode; hopCount?: number },
  ) => {
    if (!query.trim()) {
      setSearchResults([]);
      setSearchError(null);
      setSearchStats(null);
      return;
    }
    const effectiveScope = overrides?.scope ?? searchScope;
    const effectiveMode = overrides?.mode ?? matchMode;
    const effectiveHops = overrides?.hopCount ?? hops;

    try {
      setSearching(true);
      setSearchError(null);
      const result = await explorerApi.searchSubgraph({
        query: query.trim(),
        scope: effectiveScope.length > 0 ? effectiveScope : undefined,
        match_mode: effectiveMode,
        hops: effectiveHops,
        limit: 50,
      });
      if (!isMountedRef.current) return;

      const hitNodes = result.hit_nodes ?? [];
      setSearchResults(hitNodes);

      if (hitNodes.length === 0) {
        setSearchStats(t("kgBrowser.noHitNodes"));
      } else {
        setSearchStats(t("kgBrowser.hitNodesCount", { count: hitNodes.length }));
      }

      // Pass the subgraph to the parent for rendering in the Graph view
      onSubgraphSearch?.({
        query: query.trim(),
        hitNodes,
        nodes: result.nodes ?? [],
        edges: result.edges ?? [],
      });
    } catch (err) {
      if (!isMountedRef.current) return;
      console.error("Subgraph search failed:", err);
      setSearchError(t("kgBrowser.subgraphSearchFailed"));
      setSearchStats(null);
    } finally {
      if (isMountedRef.current) setSearching(false);
    }
  }, [t, searchScope, matchMode, hops, onSubgraphSearch]);

  // Only show full-loading on the very first render (before any schema data).
  // On subsequent database switches, keep the sidebar visible and only show
  // a loading indicator inside the node-labels / relationship-types sections.
  if (schemaLoading && nodeLabels.length === 0 && relationshipTypes.length === 0) {
    return <div className={styles.sidebar}>{t("kgBrowser.databaseInformation")}...</div>;
  }

  return (
    <div className={styles.sidebar}>
      <h3 className={styles.sidebarTitle}>
        {t("kgBrowser.databaseInformation")}
      </h3>

      {/* Tab buttons */}
      <div className={styles.sidebarTabs}>
        <button
          className={`${styles.sidebarTabBtn} ${activeTab === "database" ? styles.sidebarTabActive : ""}`}
          onClick={() => setActiveTab("database")}
        >
          {t("kgBrowser.database")}
        </button>
        <button
          className={`${styles.sidebarTabBtn} ${activeTab === "search" ? styles.sidebarTabActive : ""}`}
          onClick={() => setActiveTab("search")}
        >
          {t("kgBrowser.graphSearch")}
        </button>
      </div>

      {/* Tab content */}
      {activeTab === "database" && (
        <div className={styles.sidebarContent}>
          {schemaError && <div className={styles.errorText}>{schemaError}</div>}
          {showDatabaseSelector && (
            <div className={styles.sidebarSection}>
              <div className={styles.sidebarLabel}>
                {t("kgBrowser.useDatabase")}
              </div>
              <Select
                value={selectedDatasourceCode}
                onChange={(value) => {
                  setSelectedDatasourceCode(value);
                  onDatasourceCodeChange?.(value);
                }}
                style={{ width: "100%" }}
                options={databaseOptions}
                size="small"
                loading={schemaLoading}
              />
            </div>
          )}

          <div className={styles.sidebarSection}>
            <div className={styles.sidebarLabelRow}>
              <span className={styles.sidebarLabel}>
                {t("kgBrowser.nodeLabels")}
              </span>
            </div>
            <div className={styles.tagList}>
              {schemaLoading ? (
                <div className={styles.sidebarTagLoading}>
                  <Spin size="small" />
                  <span>{t("kgBrowser.loading")}</span>
                </div>
              ) : nodeLabels.map((item) => (
                <Tag
                  key={item.label}
                  color={item.color}
                  className={styles.sidebarTag}
                  onClick={() => onLabelClick?.(item.label, item.zone)}
                >
                  {item.label === "*"
                    ? `*(${item.count.toLocaleString()})`
                    : `${item.label}`}
                </Tag>
              ))}
            </div>
          </div>

          <div className={styles.sidebarSection}>
            <div className={styles.sidebarLabelRow}>
              <span className={styles.sidebarLabel}>
                {t("kgBrowser.relationshipTypes")}
              </span>
            </div>
            <div className={styles.tagList}>
              {schemaLoading ? (
                <div className={styles.sidebarTagLoading}>
                  <Spin size="small" />
                  <span>{t("kgBrowser.loading")}</span>
                </div>
              ) : relationshipTypes.map((item) => (
                <Tag
                  key={item.type}
                  className={styles.sidebarTag}
                  onClick={() => onRelTypeClick?.(item.type, item.zone)}
                >
                  {item.type === "*"
                    ? `*(${item.count.toLocaleString()})`
                    : item.type}
                </Tag>
              ))}
            </div>
          </div>
        </div>
      )}

      {activeTab === "search" && (
        <div className={styles.sidebarContent}>
          {searchError && <div className={styles.errorText}>{searchError}</div>}

          {/* Search configuration */}
          <div className={styles.searchConfig}>
            {/* Search Scope — multi-select zones */}
            <div className={styles.searchConfigRow}>
              <span className={styles.searchConfigLabel}>
                {t("kgBrowser.searchScope")}
              </span>
              <div className={styles.searchConfigValue}>
                {([
                  { zone: "metadata" as Zone, label: "MG" },
                  { zone: "trace" as Zone, label: "TG" },
                  { zone: "knowledge" as Zone, label: "KG" },
                ]).map(({ zone, label }) => {
                  const isActive = searchScope.includes(zone);
                  return (
                    <button
                      key={zone}
                      className={`${styles.searchConfigBtn} ${isActive ? styles.searchConfigBtnActive : ""}`}
                      onClick={() => {
                        const newScope = isActive
                          ? searchScope.filter((z) => z !== zone)
                          : [...searchScope, zone];
                        setSearchScope(newScope);
                        if (searchQuery.trim()) handleSearch(searchQuery, { scope: newScope });
                      }}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
            </div>

            {/* Match Mode */}
            <div className={styles.searchConfigRow}>
              <span className={styles.searchConfigLabel}>
                {t("kgBrowser.matchMode")}
              </span>
              <div className={styles.searchConfigValue}>
                <button
                  className={`${styles.searchConfigBtn} ${matchMode === "exact" ? styles.searchConfigBtnActive : ""}`}
                  onClick={() => {
                    setMatchMode("exact");
                    if (searchQuery.trim()) handleSearch(searchQuery, { mode: "exact" });
                  }}
                >
                  {t("kgBrowser.exactMatch")}
                </button>
                <button
                  className={`${styles.searchConfigBtn} ${matchMode === "fuzzy" ? styles.searchConfigBtnActive : ""}`}
                  onClick={() => {
                    setMatchMode("fuzzy");
                    if (searchQuery.trim()) handleSearch(searchQuery, { mode: "fuzzy" });
                  }}
                >
                  {t("kgBrowser.fuzzyMatch")}
                </button>
              </div>
            </div>

            {/* Expand Hops */}
            <div className={styles.searchConfigRow}>
              <span className={styles.searchConfigLabel}>
                {t("kgBrowser.expandHops")}
              </span>
              <div className={styles.searchConfigValue}>
                {[1, 2, 3].map((h) => (
                  <button
                    key={h}
                    className={`${styles.searchConfigBtn} ${hops === h ? styles.searchConfigBtnActive : ""}`}
                    onClick={() => {
                      setHops(h);
                      if (searchQuery.trim()) handleSearch(searchQuery, { hopCount: h });
                    }}
                  >
                    {h}
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* Search input — moved below config, input left + button right */}
          <div className={styles.searchBox} style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <Input
              placeholder={t("kgBrowser.subgraphSearchPlaceholder")}
              value={searchQuery}
              onChange={(e) => {
                const value = e.target.value;
                setSearchQuery(value);
                if (!value.trim()) {
                  setSearchResults([]);
                  setSearchError(null);
                  setSearchStats(null);
                }
              }}
              onPressEnter={() => {
                if (searchQuery.trim()) handleSearch(searchQuery);
              }}
              disabled={searching}
              size="small"
              allowClear
              style={{ width: "80%" }}
            />
            <button
              type="button"
              className={`${styles.searchConfigBtn} ${styles.searchConfigBtnActive}`}
              style={{ width: "20%", flexShrink: 0 }}
              onClick={() => {
                if (searchQuery.trim()) handleSearch(searchQuery);
              }}
              disabled={searching || !searchQuery.trim()}
            >
              {searching ? <Spin size="small" /> : <SearchOutlined />}
            </button>
          </div>

          {/* Search stats */}
          {searchStats && (
            <div className={styles.searchStats}>{searchStats}</div>
          )}

          {/* Search results list */}
          <div className={styles.searchResults}>
            {searchResults.length === 0 && searchQuery.trim() && !searching && (
              <div className={styles.searchResultEmpty}>
                {t("kgBrowser.noSearchResults")}
              </div>
            )}
            {searchResults.map((node) => {
              const converted = apiNodeToGraphNode(node);
              const clickLabel = converted.label || converted.id;
              return (
                <div
                  key={converted.id}
                  className={styles.searchResultItem}
                  onClick={() => {
                    setSearchQuery(clickLabel);
                    handleSearch(clickLabel);
                  }}
                >
                  <Tag color={NODE_TYPE_COLORS[converted.type] || "#78909c"}>
                    {converted.type}
                  </Tag>
                  <span className={styles.searchResultName}>
                    {converted.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
