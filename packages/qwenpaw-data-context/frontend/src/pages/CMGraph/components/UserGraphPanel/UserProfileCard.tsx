import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, Descriptions, Input, Space, Tag } from 'antd';

import { Drawer, Form, message } from '@/design';
import { getErrorMessage, isFormValidationError } from '@/services/apiError';
import type { UserGraphProfile, UserGraphProfilePatch, UserGraphUser } from '@/services/userGraph';
import { formatDateTime } from '@/utils';

interface ProfileFormValues {
  display_name: string;
  description: string;
  locale: string;
  timezone: string;
}

interface UserProfileCardProps {
  user: UserGraphUser;
  canWrite: boolean;
  saving: boolean;
  onSave: (body: UserGraphProfilePatch) => Promise<UserGraphUser>;
}

const PROFILE_FIELDS = ['display_name', 'description', 'locale', 'timezone'] as const;

function initialValues(profile: UserGraphProfile | null): ProfileFormValues {
  return {
    display_name: profile?.display_name ?? '',
    description: profile?.description ?? '',
    locale: profile?.locale ?? '',
    timezone: profile?.timezone ?? '',
  };
}

function buildPatch(
  values: ProfileFormValues,
  profile: UserGraphProfile | null,
): UserGraphProfilePatch {
  const patch: UserGraphProfilePatch = {};
  for (const field of PROFILE_FIELDS) {
    const nextValue = values[field].trim() || null;
    const previousValue = profile?.[field] ?? null;
    if (nextValue !== previousValue) patch[field] = nextValue;
  }
  return patch;
}

export default function UserProfileCard({ user, canWrite, saving, onSave }: UserProfileCardProps) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ProfileFormValues>();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const profile = user.profile;

  const openEditor = () => {
    form.setFieldsValue(initialValues(profile));
    setDrawerOpen(true);
  };

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      const patch = buildPatch(values, profile);
      if (Object.keys(patch).length === 0) {
        message.info(t('kgBrowser.userGraph.noProfileChanges'));
        setDrawerOpen(false);
        return;
      }
      await onSave(patch);
      message.success(t('kgBrowser.userGraph.profileSaved'));
      setDrawerOpen(false);
    } catch (error) {
      if (isFormValidationError(error)) return;
      message.error(getErrorMessage(error, t('kgBrowser.userGraph.profileSaveFailed')));
    }
  };

  const displayValue = (value: string | null | undefined) => value || '-';

  return (
    <>
      <Card
        title={t('kgBrowser.userGraph.profile')}
        extra={
          canWrite ? (
            <Button type="link" onClick={openEditor}>
              {t('common.edit')}
            </Button>
          ) : null
        }
      >
        <Descriptions column={{ xs: 1, sm: 2, lg: 3 }} size="small">
          <Descriptions.Item label={t('kgBrowser.userGraph.displayName')}>
            {displayValue(profile?.display_name)}
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.userId')}>
            {user.user_id}
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.status')}>
            <Tag color={user.status === 'active' ? 'green' : 'default'}>
              {t(`kgBrowser.userGraph.userStatus.${user.status}`, {
                defaultValue: user.status,
              })}
            </Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.description')} span={3}>
            {displayValue(profile?.description)}
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.locale')}>
            {displayValue(profile?.locale)}
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.timezone')}>
            {displayValue(profile?.timezone)}
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.createdAt')}>
            {formatDateTime(user.created_at)}
          </Descriptions.Item>
          <Descriptions.Item label={t('kgBrowser.userGraph.updatedAt')}>
            {formatDateTime(user.updated_at)}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <Drawer
        title={t('kgBrowser.userGraph.editProfile')}
        width="min(520px, 100vw)"
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        destroyOnClose
        footer={
          <Space style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <Button onClick={() => setDrawerOpen(false)}>{t('common.cancel')}</Button>
            <Button type="primary" loading={saving} onClick={handleSave}>
              {t('common.save')}
            </Button>
          </Space>
        }
      >
        <Form form={form} layout="vertical" initialValues={initialValues(profile)}>
          <Form.Item
            name="display_name"
            label={t('kgBrowser.userGraph.displayName')}
            rules={[{ max: 128, message: t('kgBrowser.userGraph.maxLength', { count: 128 }) }]}
          >
            <Input allowClear maxLength={128} />
          </Form.Item>
          <Form.Item
            name="description"
            label={t('kgBrowser.userGraph.description')}
            rules={[{ max: 1000, message: t('kgBrowser.userGraph.maxLength', { count: 1000 }) }]}
          >
            <Input.TextArea allowClear rows={4} maxLength={1000} showCount />
          </Form.Item>
          <Form.Item
            name="locale"
            label={t('kgBrowser.userGraph.locale')}
            rules={[{ max: 32, message: t('kgBrowser.userGraph.maxLength', { count: 32 }) }]}
          >
            <Input allowClear maxLength={32} placeholder="zh-CN" />
          </Form.Item>
          <Form.Item
            name="timezone"
            label={t('kgBrowser.userGraph.timezone')}
            rules={[{ max: 64, message: t('kgBrowser.userGraph.maxLength', { count: 64 }) }]}
          >
            <Input allowClear maxLength={64} placeholder="Asia/Shanghai" />
          </Form.Item>
        </Form>
      </Drawer>
    </>
  );
}
