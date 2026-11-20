import { useEffect, useRef, type RefObject } from 'react';
import { getDatasetMeta } from '../services/datasetManagement';
import { getUiBridge } from './bridge';
import type { DatasetManagementItem, QueryDatasetMetaParams } from '../types/datasetManagement';

interface AdapterOptions {
  root: RefObject<HTMLDivElement | null>; // Registered list container only.
  rows: DatasetManagementItem[]; // Actually loaded page rows.
  canWrite: boolean; // Current permission, never inferred from model input.
  sourceOptions: { value: string | number; label: string }[]; // Real source options.
  sourcesReady: boolean; // Source request completed and its options committed.
  domainOptions: { value: string | number; label: string }[]; // Real domain options.
  loadSource: (id: string) => Promise<void>; // Existing dependency loader.
  present: (params: QueryDatasetMetaParams, target?: DatasetManagementItem) => void; // Controlled table state.
  edit: (row: DatasetManagementItem, ready: () => void) => void; // Existing drawer handler.
}
export function useDatasetAdapter(options: AdapterOptions) {
  const current = useRef(options);
  current.current = options;
  useEffect(() => {
    const bridge = getUiBridge();
    if (!bridge) return;
    return bridge.object('cm.dataset', async (ref, context) => {
      bridge.assertClean();
      const object = await getDatasetMeta(Number(ref.dataset_id), context.signal);
      context.assertCurrent();
      if (object.datasource_id !== ref.datasource_id || object.domain_id !== ref.domain_id)
        throw new Error('TARGET_NOT_FOUND');
      await current.current.loadSource(object.datasource_id);
      await bridge.wait(() => current.current.sourcesReady, context.signal);
      context.assertCurrent();
      if (
        !current.current.sourceOptions.some((item) => String(item.value) === object.datasource_id)
      )
        throw new Error('FORBIDDEN');
      current.current.present(
        { datasource_id: object.datasource_id, domain_id: object.domain_id },
        object.record,
      );
      await bridge.wait(
        () =>
          current.current.rows.some((row) => row.dataset_id === object.dataset_id) &&
          Boolean(
            current.current.root.current?.querySelector(`[data-ui-row="${object.dataset_id}"]`),
          ),
        context.signal,
      );
      context.assertCurrent();
      current.current.root.current
        ?.querySelector(`[data-ui-row="${object.dataset_id}"]`)
        ?.scrollIntoView({ block: 'center' });
      return { object_ref: ref, visible: true, view_state: 'opened', completed_steps: ['located'] };
    });
  }, []);
  useEffect(() => {
    const bridge = getUiBridge();
    if (!bridge) return;
    const cleanups: (() => void)[] = [];
    const registerFilter = (field: 'datasource' | 'domain') => {
      const items = field === 'datasource' ? options.sourceOptions : options.domainOptions;
      cleanups.push(
        bridge.register(
          {
            element_id: `dataset.filter.${field}`,
            allowed_actions: ['select'],
            value_type: 'single_select',
            options: items.map((item) => ({ id: String(item.value), label: item.label })),
          },
          async (action, context) => {
            bridge.assertClean();
            const values = action.value;
            if (
              !Array.isArray(values) ||
              values.length !== 1 ||
              !items.some((item) => String(item.value) === values[0])
            )
              throw new Error('INVALID_ARGUMENT');
            if (field === 'datasource') {
              await current.current.loadSource(values[0]);
              context.assertCurrent();
            }
            const previousRows = current.current.rows;
            current.current.present(
              field === 'datasource'
                ? { datasource_id: values[0], domain_id: undefined }
                : { domain_id: Number(values[0]) },
            );
            await bridge.wait(() => current.current.rows !== previousRows, context.signal);
            context.assertCurrent();
            return { value: values, completed_steps: ['filtered'] };
          },
        ),
      );
    };
    registerFilter('datasource');
    registerFilter('domain');
    cleanups.push(
      bridge.register(
        { element_id: 'dataset.list', allowed_actions: ['scroll'] },
        async (action, context) => {
          context.assertCurrent();
          if (action.anchor_id !== 'dataset.list' || !options.root.current)
            throw new Error('TARGET_NOT_FOUND');
          options.root.current.scrollIntoView({ block: 'start' });
          return { visible: true, completed_steps: ['scrolled'] };
        },
      ),
    );
    for (const row of options.rows) {
      const id = `dataset.row:${row.dataset_id}`;
      cleanups.push(
        bridge.register(
          { element_id: id, allowed_actions: ['focus', 'scroll'] },
          async (action, context) => {
            context.assertCurrent();
            if (action.type === 'scroll' && action.anchor_id !== id)
              throw new Error('TARGET_NOT_FOUND');
            const element = options.root.current?.querySelector<HTMLElement>(
              `[data-ui-row="${row.dataset_id}"]`,
            );
            if (!element) throw new Error('TARGET_NOT_FOUND');
            element.scrollIntoView({ block: 'center' });
            if (action.type === 'focus') element.focus();
            return { visible: true, completed_steps: [action.type] };
          },
        ),
      );
      if (options.canWrite)
        cleanups.push(
          bridge.register(
            { element_id: `${id}.edit`, allowed_actions: ['click'] },
            async (_action, context) => {
              bridge.assertClean();
              context.assertCurrent();
              if (!current.current.canWrite) throw new Error('FORBIDDEN');
              let ready = false;
              current.current.edit(row, () => {
                ready = true;
                bridge.touch();
              });
              await bridge.wait(() => ready, context.signal);
              context.assertCurrent();
              return { completed_steps: ['editor_opened'] };
            },
          ),
        );
    }
    bridge.touch();
    return () => cleanups.forEach((cleanup) => cleanup());
  }, [
    options.rows,
    options.canWrite,
    options.sourceOptions,
    options.sourcesReady,
    options.domainOptions,
    options.root,
  ]);
}
