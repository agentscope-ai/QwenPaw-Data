import { CloseOutlined } from '@ant-design/icons';
import { Button, Checkbox, Input, Popconfirm, Radio } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useAppI18n } from '@/i18n';
import type { AskUserQuestionSemanticAnswer, PendingAskUserQuestion } from '../../api/types';
import sharedStyles from '../../index.module.less';
import { mergeStyleModules } from '../../styleModules';
import ownerStyles from './index.module.less';

const styles = mergeStyleModules(ownerStyles, sharedStyles);

interface DraftAnswer {
  selectedOptions: string[];
  customSelected: boolean;
  text: string;
}

type SlideDirection = 'previous' | 'next';

const CUSTOM_OPTION_VALUE = '__qwenpaw_data_custom_answer__';

function remainingSeconds(expiresAt: string | null): number | null {
  if (!expiresAt) return null;
  const deadline = Date.parse(expiresAt);
  if (Number.isNaN(deadline)) return null;
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
}

function formatCountdown(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  if (hours > 0) {
    return [hours, minutes, remainder]
      .map((value, index) => (index === 0 ? String(value) : String(value).padStart(2, '0')))
      .join(':');
  }
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

interface RuntimeClarificationCardProps {
  embedded?: boolean;
  clarification: PendingAskUserQuestion;
  onClose: () => Promise<void>;
  onSubmit: (answers: AskUserQuestionSemanticAnswer[]) => Promise<void>;
}

export default function RuntimeClarificationCard({
  embedded = false,
  clarification,
  onClose,
  onSubmit,
}: RuntimeClarificationCardProps) {
  const { t } = useAppI18n();
  const [drafts, setDrafts] = useState<Record<string, DraftAnswer>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeQuestionIndex, setActiveQuestionIndex] = useState(0);
  const [slideDirection, setSlideDirection] = useState<SlideDirection>('next');
  const [customValidation, setCustomValidation] = useState<Record<string, boolean>>({});
  const [countdown, setCountdown] = useState<number | null>(() =>
    remainingSeconds(clarification.expires_at),
  );

  useEffect(() => {
    setDrafts({});
    setSubmitting(false);
    setError(null);
    setActiveQuestionIndex(0);
    setSlideDirection('next');
    setCustomValidation({});
    setCountdown(remainingSeconds(clarification.expires_at));
  }, [clarification.expires_at, clarification.id]);

  useEffect(() => {
    if (!clarification.expires_at) return;
    const deadline = Date.parse(clarification.expires_at);
    if (Number.isNaN(deadline)) return;
    let timer: number | undefined;
    const tick = () => {
      const milliseconds = deadline - Date.now();
      const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
      setCountdown(seconds);
      if (milliseconds <= 0) {
        return;
      }
      timer = window.setTimeout(tick, Math.min(1000, milliseconds));
    };
    tick();
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [clarification.expires_at]);

  const validationState = useMemo(
    () =>
      clarification.questions.map((question, index) => {
        const draft = drafts[String(index)];
        const answered = Boolean(draft?.selectedOptions.length || draft?.customSelected);
        const customValid = !draft?.customSelected || Boolean(draft.text.trim());
        return {
          answered,
          valid: customValid && (!question.required || answered),
        };
      }),
    [clarification.questions, drafts],
  );
  const complete = validationState.every((state) => state.valid);

  const activeQuestion = clarification.questions[activeQuestionIndex];
  const activeDraft = drafts[String(activeQuestionIndex)] ?? {
    selectedOptions: [],
    customSelected: false,
    text: '',
  };
  const activeCustomInvalid = Boolean(
    activeDraft.customSelected &&
    customValidation[String(activeQuestionIndex)] &&
    !activeDraft.text.trim(),
  );

  const updateDraft = (questionIndex: number, patch: Partial<DraftAnswer>) => {
    const key = String(questionIndex);
    setDrafts((current) => {
      const previous = current[key];
      return {
        ...current,
        [key]: {
          selectedOptions: patch.selectedOptions ?? previous?.selectedOptions ?? [],
          customSelected: patch.customSelected ?? previous?.customSelected ?? false,
          text: patch.text ?? previous?.text ?? '',
        },
      };
    });
  };

  const clearCustomValidation = (questionIndex: number) => {
    const key = String(questionIndex);
    setCustomValidation((current) => ({ ...current, [key]: false }));
  };

  const navigateToQuestion = (nextIndex: number, direction: SlideDirection) => {
    setSlideDirection(direction);
    setActiveQuestionIndex(nextIndex);
  };

  const goToNextQuestion = (questionIndex: number) => {
    const draft = drafts[String(questionIndex)];
    if (draft?.customSelected && !draft.text.trim()) {
      setCustomValidation((current) => ({ ...current, [String(questionIndex)]: true }));
      return;
    }
    if (questionIndex < clarification.questions.length - 1) {
      navigateToQuestion(questionIndex + 1, 'next');
    }
  };

  const submit = async () => {
    const answers = clarification.questions.map<AskUserQuestionSemanticAnswer>(
      (question, index) => {
        const draft = drafts[String(index)];
        return {
          question: question.question,
          selected_options: draft?.selectedOptions ?? [],
          custom_text: draft?.customSelected ? draft.text.trim() || null : null,
        };
      },
    );
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(answers);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
      setSubmitting(false);
    }
  };

  const close = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await onClose();
    } catch (closeError) {
      setError(closeError instanceof Error ? closeError.message : String(closeError));
      setSubmitting(false);
    }
  };

  return (
    <section
      className={embedded ? styles.clarificationEmbedded : styles.clarificationDock}
      aria-label={clarification.title}
    >
      <header className={styles.clarificationHeader}>
        <div className={styles.clarificationHeading}>
          <strong>{clarification.title}</strong>
        </div>
        <div className={styles.clarificationHeaderActions}>
          <span className={styles.clarificationStatus}>
            {t('runtime.waitingInput')}
            {countdown !== null
              ? ` · ${t('runtime.timeRemaining', { time: formatCountdown(countdown) })}`
              : ''}
          </span>
        </div>
      </header>
      <Popconfirm
        cancelText={t('common.cancel')}
        description={t('runtime.closeClarificationConfirmDescription')}
        disabled={submitting}
        okButtonProps={{ danger: true }}
        okText={t('runtime.closeClarificationAction')}
        placement="bottomRight"
        title={t('runtime.closeClarificationConfirmTitle')}
        onConfirm={close}
      >
        <Button
          aria-label={t('runtime.closeClarification')}
          className={styles.clarificationClose}
          icon={<CloseOutlined />}
          loading={submitting}
          size="small"
          title={t('runtime.closeClarification')}
          type="text"
        />
      </Popconfirm>

      <div className={styles.clarificationCarousel}>
        <section
          className={`${styles.clarificationQuestion} ${
            slideDirection === 'next'
              ? styles.clarificationQuestionSlideNext
              : styles.clarificationQuestionSlidePrevious
          }`}
          key={`${activeQuestionIndex}-${activeQuestion.question}`}
          role="group"
          aria-label={activeQuestion.question}
        >
          <div className={styles.clarificationQuestionTitle}>
            <span>
              <strong>{activeQuestion.question}</strong>
              {activeQuestion.required ? <em aria-label={t('runtime.required')}>*</em> : null}
            </span>
            {clarification.questions.length > 1 ? (
              <small>
                {activeQuestionIndex + 1} / {clarification.questions.length}
              </small>
            ) : null}
          </div>
          {activeQuestion.description ? <p>{activeQuestion.description}</p> : null}
          {!activeQuestion.multiSelect ? (
            <Radio.Group
              className={styles.clarificationOptionGroup}
              value={
                activeDraft.customSelected ? CUSTOM_OPTION_VALUE : activeDraft.selectedOptions[0]
              }
              onChange={(event) => {
                const value = String(event.target.value);
                const customSelected = value === CUSTOM_OPTION_VALUE;
                updateDraft(activeQuestionIndex, {
                  selectedOptions: customSelected ? [] : [value],
                  customSelected,
                });
                clearCustomValidation(activeQuestionIndex);
              }}
            >
              {activeQuestion.options.map((option, optionIndex) => (
                <Radio
                  className={`${styles.clarificationOptionCard} ${
                    activeDraft.selectedOptions.includes(option.label)
                      ? styles.clarificationOptionSelected
                      : ''
                  }`}
                  key={`${optionIndex}-${option.label}`}
                  value={option.label}
                >
                  <span>{option.label}</span>
                  {option.description ? <small>（{option.description}）</small> : null}
                </Radio>
              ))}
              <Radio
                className={`${styles.clarificationOptionCard} ${
                  activeDraft.customSelected ? styles.clarificationOptionSelected : ''
                }`}
                value={CUSTOM_OPTION_VALUE}
              >
                <span>{t('runtime.other')}</span>
              </Radio>
            </Radio.Group>
          ) : null}
          {activeQuestion.multiSelect ? (
            <Checkbox.Group
              className={styles.clarificationOptionGroup}
              value={
                activeDraft.customSelected
                  ? [...activeDraft.selectedOptions, CUSTOM_OPTION_VALUE]
                  : activeDraft.selectedOptions
              }
              onChange={(values) => {
                const normalized = values.map(String);
                updateDraft(activeQuestionIndex, {
                  selectedOptions: normalized.filter((value) => value !== CUSTOM_OPTION_VALUE),
                  customSelected: normalized.includes(CUSTOM_OPTION_VALUE),
                });
                clearCustomValidation(activeQuestionIndex);
              }}
            >
              {activeQuestion.options.map((option, optionIndex) => (
                <Checkbox
                  className={`${styles.clarificationOptionCard} ${
                    activeDraft.selectedOptions.includes(option.label)
                      ? styles.clarificationOptionSelected
                      : ''
                  }`}
                  key={`${optionIndex}-${option.label}`}
                  value={option.label}
                >
                  <span>{option.label}</span>
                  {option.description ? <small>（{option.description}）</small> : null}
                </Checkbox>
              ))}
              <Checkbox
                className={`${styles.clarificationOptionCard} ${
                  activeDraft.customSelected ? styles.clarificationOptionSelected : ''
                }`}
                value={CUSTOM_OPTION_VALUE}
              >
                <span>{t('runtime.other')}</span>
              </Checkbox>
            </Checkbox.Group>
          ) : null}
          {activeDraft.customSelected ? (
            <div className={styles.clarificationCustomAnswer}>
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                maxLength={1000}
                placeholder={t('runtime.otherAnswerPlaceholder')}
                status={activeCustomInvalid ? 'error' : undefined}
                value={activeDraft.text}
                onBlur={() => {
                  if (!activeDraft.text.trim()) {
                    setCustomValidation((current) => ({
                      ...current,
                      [String(activeQuestionIndex)]: true,
                    }));
                  }
                }}
                onChange={(event) => {
                  updateDraft(activeQuestionIndex, { text: event.target.value });
                  if (event.target.value.trim()) clearCustomValidation(activeQuestionIndex);
                }}
              />
              {activeCustomInvalid ? (
                <span role="alert">{t('runtime.otherAnswerRequired')}</span>
              ) : null}
            </div>
          ) : null}
        </section>
      </div>

      <footer className={styles.clarificationFooter}>
        {error ? (
          <span className={styles.clarificationError} role="alert">
            {error}
          </span>
        ) : (
          <span />
        )}
        <div className={styles.clarificationFooterActions}>
          {activeQuestionIndex > 0 ? (
            <Button onClick={() => navigateToQuestion(activeQuestionIndex - 1, 'previous')}>
              {t('runtime.previousQuestion')}
            </Button>
          ) : null}
          {activeQuestionIndex < clarification.questions.length - 1 ? (
            <Button onClick={() => goToNextQuestion(activeQuestionIndex)}>
              {t('runtime.nextQuestion')}
            </Button>
          ) : null}
          {complete ? (
            <Button
              className={styles.clarificationSubmit}
              type="primary"
              loading={submitting}
              onClick={() => void submit()}
            >
              {t('runtime.submitAndContinue')}
            </Button>
          ) : null}
        </div>
      </footer>
    </section>
  );
}
