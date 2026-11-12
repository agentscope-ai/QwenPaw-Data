import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode, RefObject } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { CodeOutlined } from '@/design';
import { useHasCapability } from '@/store/authStore';
import { cypherApi } from '@/services/cmgraph';
import type { RawCypherResponse } from '@/services/rawCypher';
import { useGraphCenter } from '../GraphCenterContext';
import { simulateCypher, type CypherRow } from '../dataset/graphDb';
import { usePresence } from './iosMotion';
import RawCypherPanel from './RawCypherPanel';
import styles from '../index.module.less';

export type CypherMode = 'query' | 'raw';

export interface QueryViewPayload {
  q: string;
  ids: Set<string>;
  count: number;
}

interface CypherConsoleContextValue {
  open: boolean;
  mode: CypherMode;
  canRaw: boolean;
  overlayOpen: boolean;
  cypherInput: string;
  cypherMsg: string;
  cypherRunning: boolean;
  cypherInputRef: RefObject<HTMLInputElement>;
  setConsoleOpen: (open: boolean) => void;
  setConsoleMode: (mode: CypherMode) => void;
  setCypherInput: (value: string) => void;
  runCypherQuery: () => void;
  handleRawSuccess: (result: RawCypherResponse, cypher: string) => void;
}

const CypherConsoleContext = createContext<CypherConsoleContextValue | null>(null);

function useCypherConsole() {
  const ctx = useContext(CypherConsoleContext);
  if (!ctx) throw new Error('CypherConsole components must be used inside CypherConsoleProvider');
  return ctx;
}

function collectStructuredIds(value: unknown, out: Set<string>) {
  if (value == null) return;
  if (Array.isArray(value)) {
    value.forEach((item) => collectStructuredIds(item, out));
    return;
  }
  if (typeof value !== 'object') return;
  const obj = value as Record<string, unknown>;
  const id = obj.id ?? obj.key ?? obj.identity;
  if (typeof id === 'string' && id) out.add(id);
  else if (typeof id === 'number') out.add(String(id));
  if (obj.properties) collectStructuredIds(obj.properties, out);
  Object.values(obj).forEach((nested) => {
    if (nested && typeof nested === 'object') collectStructuredIds(nested, out);
  });
}

function extractCandidateIds(rows: Record<string, unknown>[]): string[] {
  const ids = new Set<string>();
  for (const row of rows) {
    for (const value of Object.values(row)) {
      if (typeof value === 'string' && value) ids.add(value);
      collectStructuredIds(value, ids);
    }
  }
  return [...ids];
}

export function CypherConsoleProvider({
  children,
  onQueryView,
}: {
  children: ReactNode;
  onQueryView: (view: QueryViewPayload | null) => void;
}) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { db, dataset, source, datasourceId, reload } = useGraphCenter();
  const canRaw = useHasCapability('raw_cypher');

  const rawRequested = searchParams.get('cypher') === 'raw';
  const [open, setOpen] = useState(rawRequested);
  const [mode, setMode] = useState<CypherMode>(rawRequested && canRaw ? 'raw' : 'query');
  const [cypherInput, setCypherInput] = useState('MATCH (n:Metric) RETURN n LIMIT 20');
  const [cypherMsg, setCypherMsg] = useState('');
  const [cypherRunning, setCypherRunning] = useState(false);
  const cypherInputRef = useRef<HTMLInputElement>(null);

  const overlayOpen = open && mode === 'raw' && canRaw;

  useEffect(() => {
    if (!rawRequested) return;
    setOpen(true);
    if (canRaw) setMode('raw');
  }, [rawRequested, canRaw]);

  useEffect(() => {
    if (!open || mode !== 'query') return;
    const id = window.setTimeout(() => cypherInputRef.current?.focus(), 40);
    return () => window.clearTimeout(id);
  }, [open, mode]);

  const syncRawParam = (nextOpen: boolean, nextMode: CypherMode) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (nextOpen && nextMode === 'raw' && canRaw) next.set('cypher', 'raw');
        else next.delete('cypher');
        return next;
      },
      { replace: true },
    );
  };

  const setConsoleOpen = (next: boolean) => {
    setOpen(next);
    syncRawParam(next, mode);
  };

  const setConsoleMode = (next: CypherMode) => {
    const resolved = next === 'raw' && !canRaw ? 'query' : next;
    setMode(resolved);
    syncRawParam(open, resolved);
  };

  const applyRowsToGraph = (q: string, rows: CypherRow[]) => {
    const inScope = rows.filter((r) => db.scopeHas(r.id, datasourceId));
    if (!rows.length) {
      setCypherMsg(t('graphCenter.cypherNoRows'));
      return;
    }
    if (!inScope.length) {
      setCypherMsg(t('graphCenter.cypherOutOfScope', { num: rows.length }));
      return;
    }
    onQueryView({ q, ids: new Set(inScope.map((r) => r.id)), count: inScope.length });
    setCypherMsg(t('graphCenter.cypherHit', { rows: rows.length, scoped: inScope.length }));
  };

  const runCypherQuery = async () => {
    const q = cypherInput || '';
    const isQuery = /^\s*(optional\s+)?match\b/i.test(q) || /\breturn\b/i.test(q);
    if (!isQuery) {
      // Honest read-only notice — the query console never executes writes
      // (the Raw mode with a breakglass grant is the real write path).
      setCypherMsg(t('graphCenter.cypherWriteBlocked'));
      return;
    }
    setCypherRunning(true);
    try {
      let rows: CypherRow[];
      if (source === 'api') {
        const res = await cypherApi.execute({ cypher: q, limit: 20, response_format: 'graph' });
        const ids = new Set<string>();
        (res.graph?.nodes || []).forEach((n) => {
          const id = String(n.key ?? n.id ?? '');
          if (id) ids.add(id);
        });
        if (!ids.size) {
          res.rows.forEach((r) => {
            const v = r.id ?? r.key;
            if (typeof v === 'string') ids.add(v);
          });
        }
        rows = [...ids]
          .map((id) => {
            const n = db.getNode(id);
            return n ? { id: n.id, label: n.label, layer: n.layer, type: n.type } : null;
          })
          .filter((r): r is CypherRow => !!r);
      } else {
        rows = simulateCypher(dataset, q);
      }
      applyRowsToGraph(q, rows);
    } catch (err) {
      setCypherMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setCypherRunning(false);
    }
  };

  const handleRawSuccess = (result: RawCypherResponse, cypher: string) => {
    reload();
    const candidates = extractCandidateIds(result.rows);
    const rows = candidates
      .map((id) => {
        const n = db.getNode(id);
        return n ? { id: n.id, label: n.label, layer: n.layer, type: n.type } : null;
      })
      .filter((r): r is CypherRow => !!r);
    if (!rows.length) {
      setCypherMsg(t('graphCenter.cypherRawTableOnly', { rows: result.count }));
      return;
    }
    const inScope = rows.filter((r) => db.scopeHas(r.id, datasourceId));
    if (!inScope.length) {
      setCypherMsg(t('graphCenter.cypherOutOfScope', { num: rows.length }));
      return;
    }
    onQueryView({ q: cypher, ids: new Set(inScope.map((r) => r.id)), count: inScope.length });
    setCypherMsg(t('graphCenter.cypherRawHit', { rows: result.count, scoped: inScope.length }));
  };

  const value: CypherConsoleContextValue = {
    open,
    mode,
    canRaw,
    overlayOpen,
    cypherInput,
    cypherMsg,
    cypherRunning,
    cypherInputRef,
    setConsoleOpen,
    setConsoleMode,
    setCypherInput,
    runCypherQuery: () => void runCypherQuery(),
    handleRawSuccess,
  };

  return <CypherConsoleContext.Provider value={value}>{children}</CypherConsoleContext.Provider>;
}

export function CypherConsoleToggle() {
  const { t } = useTranslation();
  const {
    open,
    mode,
    canRaw,
    cypherInput,
    cypherMsg,
    cypherRunning,
    cypherInputRef,
    setConsoleOpen,
    setConsoleMode,
    setCypherInput,
    runCypherQuery,
  } = useCypherConsole();

  return (
    <div className={`${styles.cliBar} ${open ? styles.cliBarOpen : ''}`}>
      <button
        type="button"
        className={styles.cliToggle}
        aria-expanded={open}
        aria-label={t('graphCenter.cypherConsole')}
        title={t('graphCenter.cypherConsole')}
        onClick={() => setConsoleOpen(!open)}
      >
        <CodeOutlined />
        <span className={styles.cliToggleLabel}>
          <span>{t('graphCenter.cypherConsole')}</span>
        </span>
      </button>
      <div className={styles.cliReveal}>
        <div className={styles.cliRevealInner}>
          {canRaw ? (
            <div className={styles.cypherModes}>
              <button
                type="button"
                className={`${styles.cypherMode} ${mode === 'query' ? styles.cypherModeActive : ''}`}
                onClick={() => setConsoleMode('query')}
              >
                {t('graphCenter.cypherModeQuery')}
              </button>
              <button
                type="button"
                className={`${styles.cypherMode} ${mode === 'raw' ? styles.cypherModeActive : ''}`}
                onClick={() => setConsoleMode('raw')}
              >
                {t('graphCenter.cypherModeRaw')}
              </button>
            </div>
          ) : null}
          <span className={styles.cliPrompt} aria-hidden>
            ❯
          </span>
          <input
            ref={cypherInputRef as React.RefObject<HTMLInputElement>}
            className={styles.cliInput}
            value={cypherInput}
            spellCheck={false}
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="off"
            placeholder={t('graphCenter.cypherPlaceholder')}
            onChange={(e) => setCypherInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.nativeEvent.isComposing || e.key === 'Process') return;
              if (e.key === 'Enter') {
                e.preventDefault();
                if (mode === 'query') runCypherQuery();
              } else if (e.key === 'Escape') {
                e.preventDefault();
                setConsoleOpen(false);
              }
            }}
          />
          {mode === 'query' ? (
            <button
              type="button"
              className={styles.cliRun}
              disabled={cypherRunning}
              onClick={runCypherQuery}
            >
              {t('graphCenter.cypherRun')}
            </button>
          ) : null}
        </div>
      </div>
      {open && cypherMsg ? <div className={styles.cliToast}>{cypherMsg}</div> : null}
    </div>
  );
}

export function CypherConsoleOverlay() {
  const { t } = useTranslation();
  const { overlayOpen, cypherInput, handleRawSuccess } = useCypherConsole();
  const { mounted, entered } = usePresence(overlayOpen);

  if (!mounted) return null;

  return (
    <div
      className={`${styles.cypherFloat} ${entered ? styles.cypherFloatOpen : ''}`}
      aria-label={t('rawCypher.executorTitle')}
    >
      <RawCypherPanel initialCypher={cypherInput} onSuccess={handleRawSuccess} />
    </div>
  );
}
