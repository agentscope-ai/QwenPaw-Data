import { Button, Select, Tooltip } from '@agentscope-ai/design';
import { useState } from 'react';
import { useAppI18n } from '@/i18n';
import type { ConsoleChatController } from '../../model/chat/useConsoleChat';
import styles from './index.module.less';

type Props = Pick<
  ConsoleChatController,
  | 'session'
  | 'connectionStatus'
  | 'datasources'
  | 'selectedDatasourceId'
  | 'selectDatasource'
  | 'datasets'
  | 'datasetsLoading'
  | 'datasetsError'
  | 'selectedDatasetNames'
  | 'selectDatasets'
  | 'reloadDatasets'
>;

export default function RuntimeDataSelectors({ controller }: { controller: Props }) {
  const { t } = useAppI18n();
  const [datasetsOpen, setDatasetsOpen] = useState(false);
  const [tooltipOpen, setTooltipOpen] = useState(false);

  return (
    <div className={styles.runtimeDataSelectors}>
      <Select
        className={styles.runtimeDatasourceSelect}
        classNames={{ popup: { root: styles.runtimeDatasourceDropdown } }}
        key="runtime-datasource"
        popupMatchSelectWidth={320}
        size="small"
        value={controller.selectedDatasourceId ?? undefined}
        allowClear
        showSearch
        optionFilterProp="label"
        aria-label={t('runtime.selectDatasource')}
        placeholder={t('runtime.allDatasources')}
        disabled={
          Boolean(controller.session) ||
          controller.connectionStatus === 'loading' ||
          controller.connectionStatus === 'connecting'
        }
        onChange={(value) => {
          controller.selectDatasource(value ?? null);
        }}
        options={controller.datasources.map((item) => {
          const name = item.name.trim() || item.id;
          const identity = name === item.id ? name : `${name} (${item.id})`;
          const label = `${identity}${item.recommended ? ` · ${t('runtime.recommended')}` : ''}`;
          return {
            value: item.id,
            label,
            title: label,
          };
        })}
      />
      <Tooltip
        classNames={{ root: 'qwenpaw-data-overflow-tooltip', body: 'qwenpaw-data-popup-multiline' }}
        styles={{ body: { padding: '10px 16px' } }}
        open={!datasetsOpen && tooltipOpen}
        onOpenChange={(open) => setTooltipOpen(!datasetsOpen && open)}
        title={
          controller.datasetsError
            ? t('runtime.datasetLoadFailed', { message: controller.datasetsError })
            : !controller.selectedDatasourceId
              ? t('runtime.datasetDatasourceHint')
              : undefined
        }
      >
        <Select
          className={styles.runtimeDatasetSelect}
          classNames={{ popup: { root: styles.runtimeDatasourceDropdown } }}
          aria-label={t('runtime.selectDatasets')}
          mode="multiple"
          size="small"
          allowClear
          showSearch
          maxTagCount="responsive"
          popupMatchSelectWidth={320}
          optionFilterProp="label"
          placeholder={t('runtime.allDatasets')}
          value={controller.selectedDatasetNames}
          loading={controller.datasetsLoading}
          status={controller.datasetsError ? 'error' : undefined}
          disabled={
            !controller.selectedDatasourceId ||
            controller.datasetsLoading ||
            controller.connectionStatus === 'loading'
          }
          onChange={controller.selectDatasets}
          onOpenChange={(open) => {
            setDatasetsOpen(open);
            setTooltipOpen(false);
          }}
          notFoundContent={
            controller.datasetsError ? (
              <Button size="small" type="link" onClick={controller.reloadDatasets}>
                {t('runtime.retryDatasets')}
              </Button>
            ) : (
              t('runtime.noDatasets')
            )
          }
          options={controller.datasets.map((item) => ({
            value: item.name,
            label: item.name,
            title: item.description ? `${item.name} — ${item.description}` : item.name,
          }))}
        />
      </Tooltip>
    </div>
  );
}
