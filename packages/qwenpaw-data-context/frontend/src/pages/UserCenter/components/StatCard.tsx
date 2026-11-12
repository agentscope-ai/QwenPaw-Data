import type { CSSProperties, ReactNode } from 'react';

interface StatCardProps {
  icon: ReactNode;
  label: string;
  value: string | number;
  color?: string;
  onClick?: () => void;
}

const cardStyle: CSSProperties = {
  background: 'var(--qwenpaw-data-color-form-surface)',
  border: '1px solid var(--qwenpaw-data-color-border)',
  borderRadius: 12,
  padding: 24,
  boxShadow: '0 2px 10px rgba(31, 35, 41, 0.04)',
  display: 'flex',
  alignItems: 'center',
  gap: 16,
  transition: 'box-shadow 0.2s ease, transform 0.2s ease',
};

const iconBadgeStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 48,
  height: 48,
  borderRadius: 16,
  background: 'var(--qwenpaw-data-color-primary-bg)',
  color: 'var(--qwenpaw-data-color-primary-hover)',
  fontSize: 22,
  flexShrink: 0,
};

const contentStyle: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  minWidth: 0,
};

const labelStyle: CSSProperties = {
  fontSize: 12,
  lineHeight: '16px',
  color: 'var(--qwenpaw-data-color-text-secondary)',
  fontFamily: "'Inter', 'Noto Sans SC', sans-serif",
  letterSpacing: '0.01em',
  whiteSpace: 'nowrap',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
};

const StatCard: React.FC<StatCardProps> = ({ icon, label, value, color, onClick }) => {
  const valueStyle: CSSProperties = {
    fontSize: 28,
    lineHeight: '34px',
    fontWeight: 700,
    fontFamily: "'Inter', 'Noto Sans SC', sans-serif",
    color: color ?? 'var(--qwenpaw-data-color-text)',
    letterSpacing: '-0.02em',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  };

  return (
    <div style={onClick ? { ...cardStyle, cursor: 'pointer' } : cardStyle} onClick={onClick}>
      <div style={iconBadgeStyle}>{icon}</div>
      <div style={contentStyle}>
        <span style={valueStyle}>{value}</span>
        <span style={labelStyle}>{label}</span>
      </div>
    </div>
  );
};

export default StatCard;
