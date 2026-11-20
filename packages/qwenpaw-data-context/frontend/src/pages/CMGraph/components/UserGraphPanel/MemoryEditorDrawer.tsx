import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Alert, Descriptions, Input, Select, Space, Spin, Tag } from 'antd';
import { MinusCircleOutlined, PlusOutlined } from '@ant-design/icons';

import { Drawer, Form, message } from '@/design';
import { ApiRequestError, getErrorMessage, isFormValidationError } from '@/services/apiError';
import type {
  UserGraphMemory,
  UserGraphMemoryCreate,
  UserGraphMemoryPatch,
  UserGraphMemoryType,
  UserGraphReferenceInput,
} from '@/services/userGraph';
import { formatDateTime } from '@/utils';

export type MemoryDrawerMode = 'create' | 'view' | 'edit';

interface MemoryFormValues {
  type: UserGraphMemoryType;
  description: string;
  triggers: string[];
  references: UserGraphReferenceInput[];
}

interface MemoryEditorDrawerProps {
  open: boolean;
  mode: MemoryDrawerMode;
  memory: UserGraphMemory | null;
  saving: boolean;
  onClose: () => void;
  onLoad: (memoryId: string) => Promise<UserGraphMemory>;
  onCreate: (body: UserGraphMemoryCreate) => Promise<UserGraphMemory>;
  onUpdate: (memoryId: string, body: UserGraphMemoryPatch) => Promise<UserGraphMemory>;
  onRefresh: () => Promise<void>;
}

const MEMORY_TYPES: UserGraphMemoryType[] = ['principle', 'preference', 'anchored_rule', 'trigger'];

const LABELS_BY_GRAPH = {
  metadata: ['Metric', 'Dimension', 'Column'],
  knowledge: ['Event', 'Entity'],
} as const;

function editableReferences(memory: UserGraphMemory): UserGraphReferenceInput[] {
  return memory.references.map(({ graph, label, key }) => ({ graph, label, key }));
}

function formValues(memory?: UserGraphMemory | null): MemoryFormValues {
  return {
    type: memory?.type ?? 'principle',
    description: memory?.description ?? '',
    triggers: memory?.triggers ?? [],
    references: memory ? editableReferences(memory) : [],
  };
}

function normalizeValues(values: MemoryFormValues): MemoryFormValues {
  return {
    type: values.type,
    description: values.description.trim(),
    triggers: [...new Set((values.triggers ?? []).map((item) => item.trim()).filter(Boolean))],
    references: (values.references ?? []).map((reference) => ({
      graph: reference.graph,
      label: reference.label,
      key: reference.key.trim(),
    })),
  };
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function buildMemoryPatch(values: MemoryFormValues, memory: UserGraphMemory): UserGraphMemoryPatch {
  const previous = formValues(memory);
  const patch: UserGraphMemoryPatch = {};
  if (values.type !== previous.type) patch.type = values.type;
  if (values.description !== previous.description) patch.description = values.description;
  if (!sameValue(values.triggers, previous.triggers)) patch.triggers = values.triggers;
  if (!sameValue(values.references, previous.references)) patch.references = values.references;
  return patch;
}

export default function MemoryEditorDrawer({
  open,
  mode,
  memory,
  saving,
  onClose,
  onLoad,
  onCreate,
  onUpdate,
  onRefresh,
}: MemoryEditorDrawerProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<MemoryFormValues>();
  const [detail, setDetail] = useState<UserGraphMemory | null>(memory);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailReloadKey, setDetailReloadKey] = useState(0);
  const requestTokenRef = useRef(0);
  const selectedType = Form.useWatch('type', form) ?? 'principle';

  useEffect(() => {
    if (!open) return;
    const requestToken = requestTokenRef.current + 1;
    requestTokenRef.current = requestToken;
    setDetailError(null);
    if (mode === 'create') {
      setDetail(null);
      setDetailLoading(false);
      form.setFieldsValue(formValues());
      return;
    }
    if (!memory) return;
    setDetail(memory);
    setDetailLoading(true);
    form.setFieldsValue(formValues(memory));
    void onLoad(memory.id)
      .then((loaded) => {
        if (requestTokenRef.current !== requestToken) return;
        setDetail(loaded);
        form.setFieldsValue(formValues(loaded));
      })
      .catch((error) => {
        if (requestTokenRef.current !== requestToken) return;
        if (error instanceof ApiRequestError && error.status === 404) {
          void onRefresh();
        }
        setDetailError(getErrorMessage(error, t('kgBrowser.userGraph.memoryLoadFailed')));
      })
      .finally(() => {
        if (requestTokenRef.current === requestToken) setDetailLoading(false);
      });
    return () => {
      requestTokenRef.current += 1;
    };
  }, [detailReloadKey, form, memory, mode, onLoad, onRefresh, open, t]);

  const handleTypeChange = (type: UserGraphMemoryType) => {
    if (type === 'principle' || type === 'preference') {
      form.setFieldsValue({ type, triggers: [], references: [] });
    } else if (type === 'anchored_rule') {
      form.setFieldsValue({ type, triggers: [] });
    } else {
      form.setFieldValue('type', type);
    }
  };

  const validateShape = (values: MemoryFormValues) => {
    if (values.type === 'trigger' && values.triggers.length === 0) {
      throw new Error(t('kgBrowser.userGraph.triggerRequired'));
    }
    if (values.triggers.some((trigger) => trigger.length > 128)) {
      throw new Error(t('kgBrowser.userGraph.triggerLength'));
    }
    if (values.type === 'anchored_rule' && values.references.length === 0) {
      throw new Error(t('kgBrowser.userGraph.referenceRequired'));
    }
  };

  const handleSave = async () => {
    try {
      const values = normalizeValues(await form.validateFields());
      validateShape(values);
      if (mode === 'create') {
        const created = await onCreate(values);
        message.success(
          t(
            created.action === 'merged'
              ? 'kgBrowser.userGraph.memoryMerged'
              : 'kgBrowser.userGraph.memoryCreated',
          ),
        );
      } else if (mode === 'edit' && detail) {
        const patch = buildMemoryPatch(values, detail);
        if (Object.keys(patch).length === 0) {
          message.info(t('kgBrowser.userGraph.noMemoryChanges'));
          onClose();
          return;
        }
        await onUpdate(detail.id, patch);
        message.success(t('kgBrowser.userGraph.memoryUpdated'));
      }
      onClose();
    } catch (error) {
      if (isFormValidationError(error)) return;
      if (error instanceof ApiRequestError) {
        if (error.status === 404 && mode !== 'create') {
          void onRefresh();
          onClose();
        } else if (error.status === 409 && detail) {
          setDetailReloadKey((value) => value + 1);
          void onRefresh();
        }
      }
      message.error(getErrorMessage(error, t('kgBrowser.userGraph.memorySaveFailed')));
    }
  };

  const titleKey =
    mode === 'create' ? 'createMemory' : mode === 'edit' ? 'editMemory' : 'memoryDetail';

  const renderDetail = () => {
    if (detailLoading)
      return (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      );
    if (detailError)
      return (
        <Alert
          type="error"
          showIcon
          message={t('kgBrowser.userGraph.memoryLoadFailed')}
          description={detailError}
          action={
            <Button size="small" onClick={() => setDetailReloadKey((value) => value + 1)}>
              {t('common.retry')}
            </Button>
          }
        />
      );
    if (!detail) return null;
    return (
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label={t('kgBrowser.userGraph.memoryId')}>{detail.id}</Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.memoryType')}>
          {t(`kgBrowser.userGraph.memoryTypes.${detail.type}`)}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.description')}>
          {detail.description}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.status')}>
          {t(`kgBrowser.userGraph.memoryStatuses.${detail.status}`)}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.triggers')}>
          <Space wrap>
            {detail.triggers.length
              ? detail.triggers.map((item) => <Tag key={item}>{item}</Tag>)
              : '-'}
          </Space>
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.references')}>
          {detail.references.length
            ? detail.references.map((reference) => (
                <div key={`${reference.graph}:${reference.label}:${reference.key}`}>
                  {reference.graph} · {reference.label} · {reference.name || reference.key}
                </div>
              ))
            : '-'}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.sourceType')}>
          {detail.source_type}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.sourceRef')}>
          {detail.source_ref}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.sourceRefs')}>
          {detail.source_refs.length ? detail.source_refs.join(', ') : '-'}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.createdAt')}>
          {formatDateTime(detail.created_at)}
        </Descriptions.Item>
        <Descriptions.Item label={t('kgBrowser.userGraph.updatedAt')}>
          {formatDateTime(detail.updated_at)}
        </Descriptions.Item>
      </Descriptions>
    );
  };

  return (
    <Drawer
      title={t(`kgBrowser.userGraph.${titleKey}`)}
      width="min(640px, 100vw)"
      open={open}
      onClose={onClose}
      destroyOnClose
      footer={
        mode === 'view' ? null : (
          <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button onClick={onClose}>{t('common.cancel')}</Button>
            <Button
              type="primary"
              loading={saving}
              disabled={detailLoading || Boolean(detailError) || detail?.status === 'revoked'}
              onClick={handleSave}
            >
              {t('common.save')}
            </Button>
          </Space>
        )
      }
    >
      {mode === 'view' ? (
        renderDetail()
      ) : detailLoading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      ) : detailError ? (
        <Alert
          type="error"
          showIcon
          message={t('kgBrowser.userGraph.memoryLoadFailed')}
          description={detailError}
          action={
            <Button size="small" onClick={() => setDetailReloadKey((value) => value + 1)}>
              {t('common.retry')}
            </Button>
          }
        />
      ) : (
        <Form form={form} layout="vertical" initialValues={formValues(detail)}>
          <Form.Item
            name="type"
            label={t('kgBrowser.userGraph.memoryType')}
            rules={[{ required: true }]}
          >
            <Select
              options={MEMORY_TYPES.map((type) => ({
                value: type,
                label: t(`kgBrowser.userGraph.memoryTypes.${type}`),
              }))}
              onChange={handleTypeChange}
            />
          </Form.Item>
          <Form.Item
            name="description"
            label={t('kgBrowser.userGraph.description')}
            rules={[
              {
                required: true,
                whitespace: true,
                message: t('kgBrowser.userGraph.descriptionRequired'),
              },
              { min: 8, max: 500, message: t('kgBrowser.userGraph.descriptionLength') },
            ]}
          >
            <Input.TextArea rows={5} maxLength={500} showCount />
          </Form.Item>

          {selectedType === 'trigger' && (
            <Form.Item name="triggers" label={t('kgBrowser.userGraph.triggers')}>
              <Select
                mode="tags"
                maxCount={10}
                tokenSeparators={[',']}
                placeholder={t('kgBrowser.userGraph.triggersPlaceholder')}
              />
            </Form.Item>
          )}

          {(selectedType === 'anchored_rule' || selectedType === 'trigger') && (
            <Form.List name="references">
              {(fields, { add, remove }) => (
                <Space direction="vertical" size={8} style={{ width: '100%' }}>
                  <div>{t('kgBrowser.userGraph.references')}</div>
                  {fields.map(({ key, name }) => (
                    <Space key={key} align="start" wrap>
                      <Form.Item
                        name={[name, 'graph']}
                        rules={[
                          { required: true, message: t('kgBrowser.userGraph.graphRequired') },
                        ]}
                      >
                        <Select
                          style={{ width: 120 }}
                          placeholder={t('kgBrowser.userGraph.graph')}
                          options={[
                            { value: 'metadata', label: 'MG' },
                            { value: 'knowledge', label: 'KG' },
                          ]}
                          onChange={() =>
                            form.setFieldValue(['references', name, 'label'], undefined)
                          }
                        />
                      </Form.Item>
                      <Form.Item noStyle shouldUpdate>
                        {() => {
                          const graph = form.getFieldValue(['references', name, 'graph']) as
                            keyof typeof LABELS_BY_GRAPH | undefined;
                          return (
                            <Form.Item
                              name={[name, 'label']}
                              rules={[
                                { required: true, message: t('kgBrowser.userGraph.labelRequired') },
                              ]}
                            >
                              <Select
                                style={{ width: 130 }}
                                placeholder={t('kgBrowser.userGraph.label')}
                                options={(graph ? LABELS_BY_GRAPH[graph] : []).map((label) => ({
                                  value: label,
                                  label,
                                }))}
                              />
                            </Form.Item>
                          );
                        }}
                      </Form.Item>
                      <Form.Item
                        name={[name, 'key']}
                        rules={[
                          {
                            required: true,
                            whitespace: true,
                            message: t('kgBrowser.userGraph.keyRequired'),
                          },
                        ]}
                      >
                        <Input
                          style={{ width: 220 }}
                          placeholder={t('kgBrowser.userGraph.referenceKey')}
                          maxLength={512}
                        />
                      </Form.Item>
                      <Button
                        type="text"
                        danger
                        icon={<MinusCircleOutlined />}
                        onClick={() => remove(name)}
                      />
                    </Space>
                  ))}
                  <Button
                    type="dashed"
                    icon={<PlusOutlined />}
                    disabled={fields.length >= 20}
                    onClick={() => add()}
                    block
                  >
                    {t('kgBrowser.userGraph.addReference')}
                  </Button>
                </Space>
              )}
            </Form.List>
          )}
        </Form>
      )}
    </Drawer>
  );
}
