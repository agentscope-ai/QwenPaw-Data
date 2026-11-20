import React, { useMemo } from 'react';
import { CheckCircleOutlined, CloseCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { countPasswordCategories, getPasswordViolations } from './passwordValidation';

interface PasswordStrengthProps {
  password: string;
}

interface Rule {
  key: string;
  test: (pw: string) => boolean;
}

const RULES: Rule[] = [
  { key: 'passwordRules.minLength', test: (pw) => pw.length >= 8 },
  { key: 'passwordRules.categoryCount', test: (pw) => countPasswordCategories(pw) >= 3 },
  {
    key: 'passwordRules.notCommon',
    test: (pw) => !getPasswordViolations(pw).includes('commonPassword'),
  },
];

const PasswordStrength: React.FC<PasswordStrengthProps> = ({ password }) => {
  const { t } = useTranslation('translation', { keyPrefix: 'auth' });

  const results = useMemo(() => {
    return RULES.map((rule) => ({
      key: rule.key,
      passed: password.length > 0 && rule.test(password),
    }));
  }, [password]);

  const passedCount = results.filter((r) => r.passed).length;

  const strength = useMemo(() => {
    if (password.length === 0) return { level: 'none', color: '#d9d9d9', label: '' };
    if (passedCount <= 1)
      return { level: 'weak', color: '#f5222d', label: t('passwordStrength.weak') };
    if (passedCount <= 2)
      return { level: 'medium', color: '#fa8c16', label: t('passwordStrength.medium') };
    return { level: 'strong', color: '#52c41a', label: t('passwordStrength.strong') };
  }, [password, passedCount, t]);

  const barWidth = password.length === 0 ? 0 : Math.max(20, (passedCount / RULES.length) * 100);

  return (
    <div style={{ marginTop: 8 }}>
      {/* Strength bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <div
          style={{
            flex: 1,
            height: 6,
            borderRadius: 3,
            backgroundColor: '#f0f0f0',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              width: `${barWidth}%`,
              height: '100%',
              borderRadius: 3,
              backgroundColor: strength.color,
              transition: 'width 0.3s, background-color 0.3s',
            }}
          />
        </div>
        {strength.label && (
          <span style={{ fontSize: 12, color: strength.color, fontWeight: 500, minWidth: 32 }}>
            {strength.label}
          </span>
        )}
      </div>

      {/* Validation rules */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {results.map((item) => (
          <div
            key={item.key}
            style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}
          >
            {item.passed ? (
              <CheckCircleOutlined style={{ color: '#52c41a' }} />
            ) : (
              <CloseCircleOutlined style={{ color: password.length > 0 ? '#ff4d4f' : '#d9d9d9' }} />
            )}
            <span
              style={{ color: item.passed ? '#52c41a' : password.length > 0 ? '#ff4d4f' : '#999' }}
            >
              {t(item.key)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

export default PasswordStrength;
