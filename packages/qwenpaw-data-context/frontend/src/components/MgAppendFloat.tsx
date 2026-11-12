import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, CloseOutlined, Form, Input, Select, message } from '@/design';
import { MG_APPEND_LABELS, appendMgEdge, appendMgNode } from '@/services/cmgraph/mgSync';
import { filterMgAppendProperties } from '../utils/propertySafety';
import styles from './mgAppendFloat.module.less';

export type MgAppendKind = 'node' | 'edge';

const EXIT_MS = 420;

function usePresence(open: boolean) {
  const [mounted, setMounted] = useState(open);
  const [entered, setEntered] = useState(false);

  useEffect(() => {
    if (open) {
      setMounted(true);
      return;
    }
    if (!mounted) return;
    const id = window.setTimeout(() => setMounted(false), EXIT_MS);
    return () => window.clearTimeout(id);
  }, [open, mounted]);

  useEffect(() => {
    if (!mounted || !open) {
      setEntered(false);
      return;
    }
    let inner = 0;
    const outer = window.requestAnimationFrame(() => {
      inner = window.requestAnimationFrame(() => setEntered(true));
    });
    return () => {
      window.cancelAnimationFrame(outer);
      window.cancelAnimationFrame(inner);
    };
  }, [mounted, open]);

  return { mounted, entered };
}

interface MgAppendFloatProps {
  kind: MgAppendKind | null;
  onClose: () => void;
  onSuccess: () => void;
  /** canvas = Graph Center overlay; page = fixed viewport float (CMGraph). */
  variant?: 'canvas' | 'page';
}

export default function MgAppendFloat({
  kind,
  onClose,
  onSuccess,
  variant = 'canvas',
}: MgAppendFloatProps) {
  const { t } = useTranslation();
  const { mounted, entered } = usePresence(kind !== null);
  const [shownKind, setShownKind] = useState<MgAppendKind | null>(kind);
  const [appendLoading, setAppendLoading] = useState(false);
  const [nodeForm] = Form.useForm();
  const [edgeForm] = Form.useForm();

  useEffect(() => {
    if (kind) setShownKind(kind);
  }, [kind]);

  useEffect(() => {
    if (kind === 'node') nodeForm.resetFields();
    if (kind === 'edge') edgeForm.resetFields();
  }, [kind, nodeForm, edgeForm]);

  useEffect(() => {
    if (!kind) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [kind, onClose]);

  const handleAppendNode = useCallback(async () => {
    let values: { label: string; key: string; props?: string };
    try {
      values = await nodeForm.validateFields();
    } catch {
      return;
    }
    let props: Record<string, unknown> = {};
    if (values.props?.trim()) {
      try {
        const parsed: unknown = JSON.parse(values.props);
        if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') {
          message.error(t('kgBrowser.mgSync.propsMustBeObject'));
          return;
        }
        props = filterMgAppendProperties(parsed as Record<string, unknown>);
      } catch {
        message.error(t('kgBrowser.mgSync.invalidJson'));
        return;
      }
    }
    setAppendLoading(true);
    try {
      await appendMgNode(values.label, values.key.trim(), props);
      message.success(t('kgBrowser.mgSync.nodeAppended'));
      nodeForm.resetFields();
      onClose();
      onSuccess();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setAppendLoading(false);
    }
  }, [nodeForm, t, onClose, onSuccess]);

  const handleAppendEdge = useCallback(async () => {
    let values: { from_key: string; to_key: string; rel_type: string };
    try {
      values = await edgeForm.validateFields();
    } catch {
      return;
    }
    setAppendLoading(true);
    try {
      await appendMgEdge(values.from_key.trim(), values.to_key.trim(), values.rel_type.trim(), {});
      message.success(t('kgBrowser.mgSync.edgeAppended'));
      edgeForm.resetFields();
      onClose();
      onSuccess();
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setAppendLoading(false);
    }
  }, [edgeForm, t, onClose, onSuccess]);

  const displayKind = kind ?? shownKind;
  if (!mounted || !displayKind) return null;

  const isNode = displayKind === 'node';

  return (
    <div
      className={`${styles.float} ${variant === 'page' ? styles.page : styles.canvas} ${entered ? styles.open : ''}`}
      role="dialog"
      aria-label={isNode ? t('kgBrowser.mgSync.appendNode') : t('kgBrowser.mgSync.appendEdge')}
    >
      <div className={styles.head}>
        <div className={styles.titles}>
          <div className={styles.title}>
            {isNode ? t('kgBrowser.mgSync.appendNode') : t('kgBrowser.mgSync.appendEdge')}
          </div>
          <div className={styles.hint}>
            {isNode ? t('kgBrowser.mgSync.appendNodeTitle') : t('kgBrowser.mgSync.appendEdgeTitle')}
          </div>
        </div>
        <button
          type="button"
          className={styles.close}
          onClick={onClose}
          aria-label={t('common.cancel')}
        >
          <CloseOutlined />
        </button>
      </div>

      {isNode ? (
        <Form form={nodeForm} layout="vertical" preserve={false} className={styles.form}>
          <Form.Item
            name="label"
            label={t('kgBrowser.mgSync.nodeLabel')}
            rules={[{ required: true }]}
          >
            <Select options={MG_APPEND_LABELS.map((l) => ({ label: l, value: l }))} showSearch />
          </Form.Item>
          <Form.Item
            name="key"
            label={t('kgBrowser.mgSync.nodeKey')}
            rules={[{ required: true, whitespace: true }]}
          >
            <Input placeholder="met:my_datasource:my_metric" />
          </Form.Item>
          <Form.Item name="props" label={t('kgBrowser.mgSync.nodeProps')}>
            <Input.TextArea rows={4} placeholder='{"name": "..."}' />
          </Form.Item>
        </Form>
      ) : (
        <Form form={edgeForm} layout="vertical" preserve={false} className={styles.form}>
          <Form.Item
            name="from_key"
            label={t('kgBrowser.mgSync.edgeFrom')}
            rules={[{ required: true, whitespace: true }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="rel_type"
            label={t('kgBrowser.mgSync.edgeType')}
            rules={[
              { required: true },
              { pattern: /^[A-Za-z][A-Za-z0-9_]*$/, message: t('kgBrowser.mgSync.invalidRelType') },
            ]}
          >
            <Input placeholder="HAS" />
          </Form.Item>
          <Form.Item
            name="to_key"
            label={t('kgBrowser.mgSync.edgeTo')}
            rules={[{ required: true, whitespace: true }]}
          >
            <Input />
          </Form.Item>
        </Form>
      )}

      <div className={styles.actions}>
        <Button size="small" onClick={onClose}>
          {t('common.cancel')}
        </Button>
        <Button
          size="small"
          type="primary"
          loading={appendLoading}
          onClick={isNode ? handleAppendNode : handleAppendEdge}
        >
          {t('common.create')}
        </Button>
      </div>
    </div>
  );
}
