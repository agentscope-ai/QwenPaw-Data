import { Button } from '@agentscope-ai/design';
import { Alert, InputNumber, Slider } from 'antd';
import { useEffect, useId, useRef, useState } from 'react';
import { useAppI18n } from '@/i18n';
import { dataConsoleApi } from '@/features/console/api/client';
import type { RuntimeSettings } from '@/features/console/api/types';
import { minimumTriggerPercent } from '@/features/console/model/runtime/contextUsage';
import styles from './index.module.less';

export default function ContextCompressionSettings({
  initialSettings,
}: {
  initialSettings: RuntimeSettings;
}) {
  const { t } = useAppI18n();
  const sliderId = useId();
  const [settings, setSettings] = useState(initialSettings);
  const [draftPercent, setDraftPercent] = useState<number | null>(
    initialSettings.context_trigger_ratio * 100,
  );
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const minimum = minimumTriggerPercent(settings.context_reserve_ratio);
  const valid =
    draftPercent !== null &&
    Number.isFinite(draftPercent) &&
    draftPercent / 100 > settings.context_reserve_ratio &&
    draftPercent > 0 &&
    draftPercent <= 90;
  const dirty = draftPercent !== settings.context_trigger_ratio * 100;
  const edit = (value: number | null) => {
    setDraftPercent(value);
    setSaved(false);
    setSaveError(null);
  };
  const save = async () => {
    if (!valid || draftPercent === null || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const { runtime_settings } = await dataConsoleApi.updateRuntimeSettings({
        context_trigger_ratio: draftPercent / 100,
      });
      if (!mounted.current) return;
      setSettings(runtime_settings);
      setDraftPercent(runtime_settings.context_trigger_ratio * 100);
      setSaved(true);
    } catch (error) {
      if (mounted.current) setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      if (mounted.current) setSaving(false);
    }
  };

  return (
    <section className={`${styles.section} qwenpaw-data-card-primary`}>
      <div className={styles.sectionHeading}>
        <h3>{t('contextUsage.configuration')}</h3>
      </div>
      <div className={styles.compressionConfiguration}>
        <div className={styles.settingTitle}>
          <label htmlFor={sliderId}>{t('contextUsage.threshold')}</label>
          <InputNumber
            id={sliderId}
            aria-label={t('contextUsage.threshold')}
            min={minimum}
            max={90}
            step={1}
            precision={0}
            value={draftPercent}
            suffix="%"
            disabled={saving}
            onChange={edit}
          />
        </div>
        <Slider
          ariaLabelForHandle={t('contextUsage.threshold')}
          min={minimum}
          max={90}
          step={1}
          value={draftPercent ?? minimum}
          marks={{ [minimum]: `${minimum}%`, ...(minimum < 50 ? { 50: '50%' } : {}), 90: '90%' }}
          tooltip={{ formatter: (value) => `${value}%` }}
          disabled={saving || minimum > 90}
          onChange={edit}
        />
        {!valid ? (
          <div className={styles.validation} role="alert">
            {t('contextUsage.range', { reserve: settings.context_reserve_ratio * 100 })}
          </div>
        ) : null}
        <p className={styles.hint}>{t('contextUsage.appliesNext')}</p>
        <div className={styles.footer}>
          <span role="status">{saved ? t('contextUsage.saved') : ''}</span>
          <div className={styles.actions}>
            {dirty ? (
              <Button disabled={saving} onClick={() => edit(settings.context_trigger_ratio * 100)}>
                {t('common.cancel')}
              </Button>
            ) : null}
            <Button
              type="primary"
              loading={saving}
              disabled={!valid || !dirty}
              onClick={() => void save()}
            >
              {t('common.save')}
            </Button>
          </div>
        </div>
        {saveError ? (
          <Alert type="error" showIcon message={t('common.saveFailed', { message: saveError })} />
        ) : null}
      </div>
    </section>
  );
}
