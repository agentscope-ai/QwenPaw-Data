import React, { useMemo } from 'react';
import { CheckOutlined, MinusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '@/store/authStore';
import { checkPermission } from '@/auth';
import type { Role, Resource } from '@/auth';
import type { CSSProperties } from 'react';

interface PermissionMatrixProps {
  role?: Role;
}

interface PermissionGroup {
  key: string;
  resources: Resource[];
}

const PERMISSION_GROUPS: PermissionGroup[] = [
  {
    key: 'knowledgeGraph',
    resources: [
      'mg:read',
      'mg:write',
      'mg_copy:self',
      'mg_userspace:sync',
      'mg_userspace:append',
      'tg_kg:self',
      'tg_kg:others_read',
    ],
  },
  {
    key: 'workspace',
    resources: ['workspace:manage', 'user:manage'],
  },
  {
    key: 'datasource',
    resources: ['datasource:read', 'datasource:write'],
  },
  {
    key: 'skills',
    resources: ['skill:publish', 'skill:install'],
  },
  {
    key: 'snapshot',
    resources: ['snapshot:create', 'snapshot:rollback_mg', 'snapshot:rollback_self'],
  },
  {
    key: 'advanced',
    resources: ['raw_cypher', 'semantic_weaving', 'excel_import', 'model_config', 'ops:logs'],
  },
];

const FONT_FAMILY = "'Inter', 'Noto Sans SC', sans-serif";

const groupRowStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 16,
  padding: '12px 0',
};

const groupLabelStyle: CSSProperties = {
  flexShrink: 0,
  width: 120,
  paddingTop: 3,
  fontSize: 13,
  fontWeight: 600,
  color: '#475569',
  fontFamily: FONT_FAMILY,
};

const pillListStyle: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 8,
  minWidth: 0,
};

const pillBaseStyle: CSSProperties = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 6,
  padding: '3px 12px',
  borderRadius: 999,
  fontSize: 12,
  lineHeight: '20px',
  fontFamily: FONT_FAMILY,
  whiteSpace: 'nowrap',
};

const allowedPillStyle: CSSProperties = {
  ...pillBaseStyle,
  background: 'rgba(16, 185, 129, 0.08)',
  border: '1px solid rgba(16, 185, 129, 0.25)',
  color: '#059669',
};

const deniedPillStyle: CSSProperties = {
  ...pillBaseStyle,
  background: 'rgba(239, 68, 68, 0.06)',
  border: '1px dashed rgba(239, 68, 68, 0.3)',
  color: '#f87171',
};

const PermissionMatrix: React.FC<PermissionMatrixProps> = ({ role: roleProp }) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });
  const activeWorkspaceRole = useAuthStore((state) => state.activeWorkspaceRole);
  const capabilities = useAuthStore((state) => state.capabilities);
  const activeRole = roleProp ?? activeWorkspaceRole;

  const matrix = useMemo(() => {
    return PERMISSION_GROUPS.map((group) => ({
      ...group,
      permissions: group.resources.map((resource) => ({
        resource,
        allowed: roleProp ? checkPermission(activeRole, resource) : capabilities.includes(resource),
      })),
    }));
  }, [activeRole, capabilities, roleProp]);

  return (
    <div>
      {matrix.map((group, index) => (
        <div
          key={group.key}
          style={
            index < matrix.length - 1
              ? { ...groupRowStyle, borderBottom: '1px solid rgba(226, 232, 240, 0.6)' }
              : groupRowStyle
          }
        >
          <span style={groupLabelStyle}>{t(`permissions.group.${group.key}`)}</span>
          <div style={pillListStyle}>
            {group.permissions.map(({ resource, allowed }) => (
              <span key={resource} style={allowed ? allowedPillStyle : deniedPillStyle}>
                {allowed ? (
                  <CheckOutlined style={{ fontSize: 10 }} />
                ) : (
                  <MinusOutlined style={{ fontSize: 10 }} />
                )}
                {/* ":" is i18next's ns separator — disable it so "mg:read" style keys resolve */}
                {t(`permissions.resource.${resource}`, { nsSeparator: false })}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
};

export default PermissionMatrix;
