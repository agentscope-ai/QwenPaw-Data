import { Button } from 'antd';
import { useState } from 'react';
import { useAppI18n } from '@/i18n';
import type {
  ChatStatus,
  PendingToolConfirmation,
  ToolConfirmationDecision,
} from '../../api/types';
import { toolConfirmationAvailability } from '../../model/chat/toolConfirmation';
import type { ConfirmationSubmission } from '../../model/chat/toolConfirmationSubmission';
import styles from './index.module.less';

interface Props {
  confirmation: PendingToolConfirmation;
  submission?: ConfirmationSubmission;
  status: ChatStatus;
  readOnly: boolean;
  stopping: boolean;
  ready: boolean;
  onDecide: (decision: ToolConfirmationDecision) => Promise<void>;
}

export default function RuntimeToolConfirmationCard(props: Props) {
  const { confirmation, submission, onDecide } = props;
  const { t } = useAppI18n();
  const [error, setError] = useState(false);
  const availability = toolConfirmationAvailability(confirmation, props);
  const pending = availability === 'pending';
  const canSubmit = pending && (!submission || submission.status === 'retry');
  const state =
    !pending && (availability !== 'expired' || !submission || submission.status === 'retry')
      ? availability
      : submission?.status === 'accepted'
        ? submission.decision
        : (submission?.status ?? 'pending');
  const decide = (decision: ToolConfirmationDecision) => {
    setError(false);
    void onDecide(decision).catch(() => setError(true));
  };
  const deadline = confirmation.expiresAt ? Date.parse(confirmation.expiresAt) : NaN;
  return (
    <section className={styles.card} aria-label={t('toolConfirmation.title')}>
      <header>
        <strong>{t('toolConfirmation.title')}</strong>
        <span>{confirmation.name}</span>
      </header>
      <details open={confirmation.argumentsText.length <= 400}>
        <summary>{t('toolConfirmation.arguments')}</summary>
        <pre>{confirmation.argumentsText}</pre>
      </details>
      {Number.isFinite(deadline) ? (
        <small>
          {t('toolConfirmation.deadline', {
            time: new Date(deadline).toLocaleString(),
          })}
        </small>
      ) : null}
      {state !== 'expired' ? <p role="status">{t(`toolConfirmation.${state}`)}</p> : null}
      {error ? <p role="alert">{t('toolConfirmation.unavailable')}</p> : null}
      {!props.readOnly ? (
        <footer>
          <Button disabled={!canSubmit} onClick={() => decide('denied')}>
            {t('toolConfirmation.deny')}
          </Button>
          <Button
            type="primary"
            disabled={!canSubmit}
            loading={submission?.status === 'submitting'}
            onClick={() => decide('approved')}
          >
            {t('toolConfirmation.approve')}
          </Button>
        </footer>
      ) : null}
    </section>
  );
}
