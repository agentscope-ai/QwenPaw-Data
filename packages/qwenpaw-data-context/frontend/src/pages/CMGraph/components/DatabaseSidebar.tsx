import { useEffect, useState, useCallback, useRef, type ReactNode } from 'react';
import { Select, Tag, Input, Spin, Button, Radio, Empty, Skeleton, Tooltip } from '@/design';
import { SearchOutlined, AimOutlined } from '@/design';
import { useTranslation } from 'react-i18next';

import { explorerApi } from '@/services/cmgraph';
import { apiNodeToGraphNode } from '@/services/cmgraph';
import { queryDataSourceList } from '@/services/dataSource';
import type { DataSourceItem } from '@/types/dataSource';
import { normalizeListResponse } from '@/utils/listResponse';
import type {
  SchemaNodeLabel,
  SchemaRelType,
  ApiGraphNode,
  ApiGraphEdge,
  ExplorerZone,
  MatchMode,
} from '@/services/cmgraph';
import { NODE_TYPE_COLORS } from '../nodeColors';
import styles from '../index.module.less';

type SidebarTab = 'database' | 'search';

async function fetchAllDataSources(): Promise<DataSourceItem[]> {
  const result = await queryDataSourceList({ page: 1, size: 500 });
  return normalizeListResponse<DataSourceItem>(result);
}

/** Short zone badge shown on search result cards. */
const ZONE_BADGES: Record<string, string> = {
  metadata: 'MG',
  user: 'UG',
  knowledge: 'KG',
};

/** Magnifier-over-subgraph illustration for the search tab empty state.
 *  Neutral strokes follow the container color; accent parts use the brand
 *  color via the searchEmptyIconAccent class. */
function SearchEmptyIllustration() {
  return (
    <svg width="88" height="88" viewBox="0 0 96 96" fill="none" aria-hidden="true">
      {/* Satellite node linked from inside the lens */}
      <line
        x1="60"
        y1="30"
        x2="71"
        y2="24"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <circle cx="75" cy="22" r="4" stroke="currentColor" strokeWidth="2.5" />
      {/* Lens and handle */}
      <circle cx="42" cy="42" r="26" stroke="currentColor" strokeWidth="3" />
      <line
        x1="61"
        y1="61"
        x2="78"
        y2="78"
        stroke="currentColor"
        strokeWidth="4.5"
        strokeLinecap="round"
      />
      {/* Subgraph inside the lens */}
      <g className={styles.searchEmptyIconAccent}>
        <path
          d="M29 51 L40 38 L48 46 L56 32"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx="29" cy="51" r="3.5" fill="currentColor" />
        <circle cx="40" cy="38" r="3.5" fill="currentColor" />
        <circle cx="48" cy="46" r="3.5" fill="currentColor" />
        <circle cx="56" cy="32" r="3.5" fill="currentColor" />
      </g>
    </svg>
  );
}

/** Render text with all case-insensitive keyword matches highlighted. */
function renderHighlighted(text: string, keyword: string, markClass: string): ReactNode {
  const trimmed = keyword.trim();
  if (!trimmed) return text;
  const lowerText = text.toLowerCase();
  const lowerKeyword = trimmed.toLowerCase();
  const parts: ReactNode[] = [];
  let cursor = 0;
  let index = lowerText.indexOf(lowerKeyword);
  while (index >= 0) {
    if (index > cursor) parts.push(text.slice(cursor, index));
    parts.push(
      <mark key={index} className={markClass}>
        {text.slice(index, index + trimmed.length)}
      </mark>,
    );
    cursor = index + trimmed.length;
    index = lowerText.indexOf(lowerKeyword, cursor);
  }
  if (parts.length === 0) return text;
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
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
  /** The currently active graph zone (kg/mg). */
  activeZone?: string;
  /** Restrict the sidebar to the MG layer (global MG entry view): hide the
   *  KG/TG schema tags and lock graph search scope to MG. */
  mgOnly?: boolean;
  /** Exact owner scope for all Explorer reads (`__global__` for global MG). */
  targetOwnerId?: string;
  /** Called when a node-label tag is clicked. */
  onLabelClick?: (label: string, zone?: string) => void;
  /** Called when a relationship-type tag is clicked. */
  onRelTypeClick?: (relType: string, zone?: string) => void;
  /** Called with the datasource code selected for TG/MG requests. */
  onDatasourceCodeChange?: (datasourceCode?: string) => void;
  /** Called when a subgraph search completes, passing the full subgraph data. */
  onSubgraphSearch?: (result: SubgraphSearchResult) => void;
  /** Called when a search result item is clicked to locate the node in the canvas. */
  onNodeLocate?: (nodeKey: string) => void;
}

export default function DatabaseSidebar({
  showDatabaseSelector,
  mgOnly = false,
  targetOwnerId,
  onLabelClick,
  onRelTypeClick,
  onDatasourceCodeChange,
  onSubgraphSearch,
  onNodeLocate,
}: DatabaseSidebarProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<SidebarTab>('database');

  // Database tab state
  const [selectedDatasourceCode, setSelectedDatasourceCode] = useState<string>();
  const [databaseOptions, setDatabaseOptions] = useState<{ label: string; value: string }[]>([]);
  const [nodeLabels, setNodeLabels] = useState<
    { label: string; count: number; color: string; zone?: string }[]
  >([]);
  const [relationshipTypes, setRelationshipTypes] = useState<
    { type: string; count: number; zone?: string }[]
  >([]);
  // Schema loading state — only affects the database tab's label/rel sections,
  // not the entire sidebar.
  const [schemaLoading, setSchemaLoading] = useState(true);

  // Search tab state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<ApiGraphNode[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchStats, setSearchStats] = useState<string | null>(null);
  // Last submitted query — drives result keyword highlighting.
  const [submittedQuery, setSubmittedQuery] = useState('');

  // Search configuration. The global MG entry is MG-only, so its scope is
  // locked to the metadata layer.
  const [searchScope, setSearchScope] = useState<ExplorerZone[]>(
    mgOnly ? ['metadata'] : ['metadata', 'knowledge'],
  );
  const [matchMode, setMatchMode] = useState<MatchMode>('fuzzy');
  const [hops, setHops] = useState(1);

  // Error states
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Unmount protection ref
  const isMountedRef = useRef(true);
  const targetOwnerIdRef = useRef(targetOwnerId);
  const ownerGenerationRef = useRef(0);
  if (targetOwnerIdRef.current !== targetOwnerId) {
    targetOwnerIdRef.current = targetOwnerId;
    ownerGenerationRef.current += 1;
  }
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  // Keep the search scope in sync when the MG-only context changes (an admin
  // switching between the global MG entry and browsing a member's graphs).
  useEffect(() => {
    setSearchScope(mgOnly ? ['metadata'] : ['metadata', 'knowledge']);
  }, [mgOnly]);

  useEffect(() => {
    setSearchResults([]);
    setSearchStats(null);
    setSearchError(null);
    setSearching(false);
    setSubmittedQuery('');
  }, [targetOwnerId]);

  // Load options from semantic-config so graph import failures do not make
  // the datasource selector unusable. Requests use the stable datasource_id.
  useEffect(() => {
    let cancelled = false;
    fetchAllDataSources()
      .then((datasources) => {
        if (cancelled) return;
        const options = datasources.map((datasource) => ({
          label: datasource.datasource_name,
          value: datasource.datasource_id,
        }));
        setDatabaseOptions(options);
        const firstDatasourceCode = options[0]?.value;
        setSelectedDatasourceCode(firstDatasourceCode);
        onDatasourceCodeChange?.(firstDatasourceCode);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error('Failed to load database list:', err);
      });
    return () => {
      cancelled = true;
    };
  }, [onDatasourceCodeChange]);

  // Schema is global: the endpoint does not accept a database parameter.
  useEffect(() => {
    let cancelled = false;
    setSchemaLoading(true);
    setSchemaError(null);
    explorerApi
      .getSchema(targetOwnerId ? { owner_id: targetOwnerId } : {})
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
          color: NODE_TYPE_COLORS[item.label] || '#78909c',
          zone: item.zone,
        }));
        if (mappedLabels.length > 0) {
          mappedLabels.unshift({
            label: '*',
            count: totalNodeCount,
            color: NODE_TYPE_COLORS['*'] || '#78909c',
            zone: undefined,
          });
        }

        const totalRelCount = schema.relationship_types.reduce((sum, r) => sum + r.count, 0);
        const mappedRels: { type: string; count: number; zone?: string }[] =
          schema.relationship_types.map((item: SchemaRelType) => ({
            type: item.type,
            count: item.count,
            zone: item.zone === 'cross' ? item.source_zone : item.zone,
          }));
        if (mappedRels.length > 0) {
          mappedRels.unshift({ type: '*', count: totalRelCount, zone: undefined });
        }

        setNodeLabels(mappedLabels);
        setRelationshipTypes(mappedRels);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error('Failed to fetch schema:', err);
        setSchemaError(t('kgBrowser.schemaLoadFailed'));
      })
      .finally(() => {
        if (cancelled) return;
        setSchemaLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [t, targetOwnerId]);

  // Subgraph search logic — uses searchSubgraph API for n-hop expansion.
  // Accepts optional overrides so config button clicks can trigger a search
  // with the newly selected value before state has updated.
  const handleSearch = useCallback(
    async (
      query: string,
      overrides?: { scope?: ExplorerZone[]; mode?: MatchMode; hopCount?: number },
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
      const requestGeneration = ownerGenerationRef.current;
      const requestOwnerId = targetOwnerId;

      try {
        setSearching(true);
        setSearchError(null);
        const result = await explorerApi.searchSubgraph({
          query: query.trim(),
          scope: effectiveScope.length > 0 ? effectiveScope : undefined,
          match_mode: effectiveMode,
          hops: effectiveHops,
          limit: 50,
          ...(requestOwnerId ? { owner_id: requestOwnerId } : {}),
        });
        if (
          !isMountedRef.current ||
          ownerGenerationRef.current !== requestGeneration ||
          targetOwnerIdRef.current !== requestOwnerId
        )
          return;

        const hitNodes = result.hit_nodes ?? [];
        setSearchResults(hitNodes);
        setSubmittedQuery(query.trim());

        if (hitNodes.length === 0) {
          setSearchStats(t('kgBrowser.noHitNodes'));
        } else {
          setSearchStats(t('kgBrowser.hitNodesCount', { count: hitNodes.length }));
        }

        // Pass the subgraph to the parent for rendering in the Graph view
        onSubgraphSearch?.({
          query: query.trim(),
          hitNodes,
          nodes: result.nodes ?? [],
          edges: result.edges ?? [],
        });
      } catch (err) {
        if (
          !isMountedRef.current ||
          ownerGenerationRef.current !== requestGeneration ||
          targetOwnerIdRef.current !== requestOwnerId
        )
          return;
        console.error('Subgraph search failed:', err);
        setSearchError(t('kgBrowser.subgraphSearchFailed'));
        setSearchStats(null);
      } finally {
        if (
          isMountedRef.current &&
          ownerGenerationRef.current === requestGeneration &&
          targetOwnerIdRef.current === requestOwnerId
        ) {
          setSearching(false);
        }
      }
    },
    [t, searchScope, matchMode, hops, targetOwnerId, onSubgraphSearch],
  );

  // Only show full-loading on the very first render (before any schema data).
  // On subsequent database switches, keep the sidebar visible and only show
  // a loading indicator inside the node-labels / relationship-types sections.
  if (schemaLoading && nodeLabels.length === 0 && relationshipTypes.length === 0) {
    return <div className={styles.sidebar}>{t('kgBrowser.databaseInformation')}...</div>;
  }

  // The global MG entry is an MG-only view, so KG/TG schema tags are hidden to
  // avoid switching into a layer that no longer has a visible tab.
  const visibleNodeLabels = mgOnly
    ? nodeLabels.filter((item) => !item.zone || item.zone === 'metadata')
    : nodeLabels;
  const visibleRelationshipTypes = mgOnly
    ? relationshipTypes.filter((item) => !item.zone || item.zone === 'metadata')
    : relationshipTypes;

  return (
    <div className={styles.sidebar}>
      <h3 className={styles.sidebarTitle}>{t('kgBrowser.databaseInformation')}</h3>

      {/* Tab buttons */}
      <div className={styles.sidebarTabs}>
        <button
          className={`${styles.sidebarTabBtn} ${activeTab === 'database' ? styles.sidebarTabActive : ''}`}
          onClick={() => setActiveTab('database')}
        >
          {t('kgBrowser.database')}
        </button>
        <button
          className={`${styles.sidebarTabBtn} ${activeTab === 'search' ? styles.sidebarTabActive : ''}`}
          onClick={() => setActiveTab('search')}
        >
          {t('kgBrowser.graphSearch')}
        </button>
      </div>

      {/* Tab content */}
      {activeTab === 'database' && (
        <div className={styles.sidebarContent}>
          {schemaError && <div className={styles.errorText}>{schemaError}</div>}
          {showDatabaseSelector && (
            <div className={styles.sidebarSection}>
              <div className={styles.sidebarLabel}>{t('kgBrowser.useDatabase')}</div>
              <Select
                value={selectedDatasourceCode}
                onChange={(value) => {
                  setSelectedDatasourceCode(value);
                  onDatasourceCodeChange?.(value);
                }}
                style={{ width: '100%' }}
                options={databaseOptions}
                size="small"
                loading={schemaLoading}
              />
            </div>
          )}

          <div className={styles.sidebarSection}>
            <div className={styles.sidebarLabelRow}>
              <span className={styles.sidebarLabel}>{t('kgBrowser.nodeLabels')}</span>
            </div>
            <div className={styles.tagList}>
              {schemaLoading ? (
                <div className={styles.sidebarTagLoading}>
                  <Spin size="small" />
                  <span>{t('kgBrowser.loading')}</span>
                </div>
              ) : (
                visibleNodeLabels.map((item) => (
                  <Tag
                    key={item.label}
                    color={item.color}
                    className={styles.sidebarTag}
                    onClick={() => onLabelClick?.(item.label, item.zone)}
                  >
                    {item.label === '*' ? `*(${item.count.toLocaleString()})` : `${item.label}`}
                  </Tag>
                ))
              )}
            </div>
          </div>

          <div className={styles.sidebarSection}>
            <div className={styles.sidebarLabelRow}>
              <span className={styles.sidebarLabel}>{t('kgBrowser.relationshipTypes')}</span>
            </div>
            <div className={styles.tagList}>
              {schemaLoading ? (
                <div className={styles.sidebarTagLoading}>
                  <Spin size="small" />
                  <span>{t('kgBrowser.loading')}</span>
                </div>
              ) : (
                visibleRelationshipTypes.map((item) => (
                  <Tag
                    key={item.type}
                    className={styles.sidebarTag}
                    onClick={() => onRelTypeClick?.(item.type, item.zone)}
                  >
                    {item.type === '*' ? `*(${item.count.toLocaleString()})` : item.type}
                  </Tag>
                ))
              )}
            </div>
          </div>
        </div>
      )}

      {activeTab === 'search' && (
        <div className={styles.sidebarContent}>
          {searchError && <div className={styles.errorText}>{searchError}</div>}

          {/* Search bar — input with attached primary search button */}
          <div className={styles.searchBar}>
            <Input
              placeholder={t('kgBrowser.subgraphSearchPlaceholder')}
              value={searchQuery}
              onChange={(e) => {
                const value = e.target.value;
                setSearchQuery(value);
                if (!value.trim()) {
                  setSearchResults([]);
                  setSearchError(null);
                  setSearchStats(null);
                  setSubmittedQuery('');
                }
              }}
              onPressEnter={() => {
                if (searchQuery.trim()) handleSearch(searchQuery);
              }}
              size="small"
              allowClear
            />
            <Button
              type="primary"
              size="small"
              icon={<SearchOutlined />}
              loading={searching}
              disabled={!searchQuery.trim()}
              onClick={() => {
                if (searchQuery.trim()) handleSearch(searchQuery);
              }}
              aria-label={t('kgBrowser.searchSubgraph')}
            />
          </div>

          {/* Filter card — scope chips + match mode / hops radio groups */}
          <div className={styles.searchFilterCard}>
            <div className={styles.searchFilterRow}>
              <span className={styles.searchFilterLabel}>{t('kgBrowser.searchScope')}</span>
              <div className={styles.searchChipGroup}>
                {(mgOnly
                  ? [{ zone: 'metadata' as ExplorerZone, label: 'MG' }]
                  : [
                      { zone: 'metadata' as ExplorerZone, label: 'MG' },
                      { zone: 'user' as ExplorerZone, label: 'UG' },
                      { zone: 'knowledge' as ExplorerZone, label: 'KG' },
                    ]
                ).map(({ zone, label }) => {
                  const isActive = searchScope.includes(zone);
                  return (
                    <button
                      key={zone}
                      type="button"
                      className={`${styles.searchChip} ${isActive ? styles.searchChipActive : ''}`}
                      onClick={() => {
                        // MG-only view: the scope is locked to metadata, so
                        // just (re)run the search instead of toggling it off.
                        if (mgOnly) {
                          if (searchQuery.trim())
                            handleSearch(searchQuery, { scope: ['metadata'] });
                          return;
                        }
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

            <div className={styles.searchFilterRow}>
              <span className={styles.searchFilterLabel}>{t('kgBrowser.matchMode')}</span>
              <Radio.Group
                className={styles.searchRadioGroup}
                size="small"
                optionType="button"
                buttonStyle="solid"
                value={matchMode}
                onChange={(e) => {
                  const mode = e.target.value as MatchMode;
                  setMatchMode(mode);
                  if (searchQuery.trim()) handleSearch(searchQuery, { mode });
                }}
                options={[
                  { label: t('kgBrowser.exactMatch'), value: 'exact' },
                  { label: t('kgBrowser.fuzzyMatch'), value: 'fuzzy' },
                ]}
              />
            </div>

            <div className={styles.searchFilterRow}>
              <span className={styles.searchFilterLabel}>{t('kgBrowser.expandHops')}</span>
              <Radio.Group
                className={styles.searchRadioGroup}
                size="small"
                optionType="button"
                buttonStyle="solid"
                value={hops}
                onChange={(e) => {
                  const hopCount = e.target.value as number;
                  setHops(hopCount);
                  if (searchQuery.trim()) handleSearch(searchQuery, { hopCount });
                }}
                options={[1, 2, 3].map((h) => ({ label: String(h), value: h }))}
              />
            </div>
          </div>

          {/* Results area — skeleton / result cards / empty states */}
          {searching ? (
            <Skeleton
              active
              title={false}
              paragraph={{ rows: 3 }}
              className={styles.searchSkeleton}
            />
          ) : searchResults.length > 0 ? (
            <>
              {searchStats && <div className={styles.searchStats}>{searchStats}</div>}
              <div className={styles.searchResults}>
                {searchResults.map((node) => {
                  const converted = apiNodeToGraphNode(node);
                  const zoneBadge = node.zone ? ZONE_BADGES[node.zone] : undefined;
                  return (
                    <div
                      key={converted.id}
                      className={styles.searchResultCard}
                      onClick={() => onNodeLocate?.(converted.id)}
                    >
                      <span
                        className={styles.searchResultDot}
                        style={{ background: NODE_TYPE_COLORS[converted.type] || '#78909c' }}
                      />
                      <div className={styles.searchResultMain}>
                        <span className={styles.searchResultCaption}>{converted.type}</span>
                        <span className={styles.searchResultName}>
                          {renderHighlighted(
                            converted.label || converted.id,
                            submittedQuery,
                            styles.searchHighlight,
                          )}
                        </span>
                      </div>
                      {zoneBadge && <span className={styles.zoneBadge}>{zoneBadge}</span>}
                      <Tooltip title={t('kgBrowser.locateInCanvas')}>
                        <AimOutlined className={styles.searchResultLocate} />
                      </Tooltip>
                    </div>
                  );
                })}
              </div>
            </>
          ) : searchStats !== null ? (
            <Empty
              description={t('kgBrowser.noSearchResults')}
              image={Empty.PRESENTED_IMAGE_SIMPLE}
            />
          ) : (
            <div className={styles.searchEmptyState}>
              <SearchEmptyIllustration />
              <span className={styles.searchEmptyHint}>{t('kgBrowser.searchEmptyHint')}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
