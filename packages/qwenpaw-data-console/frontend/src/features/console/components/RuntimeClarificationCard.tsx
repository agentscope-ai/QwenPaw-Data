import { LeftOutlined, RightOutlined } from '@ant-design/icons';
import { Button, Checkbox, Input, Radio } from 'antd';
import { useEffect, useMemo, useRef, useState } from 'react';
import type {
  AskUserQuestionSemanticAnswer,
  PendingAskUserQuestion,
} from '../api/types';
import styles from '../index.module.less';

interface DraftAnswer {
  selectedOptions: string[];
  customSelected: boolean;
  text: string;
}

type SlideDirection = 'previous' | 'next';

const CUSTOM_OPTION_VALUE = '__datapaw_custom_answer__';

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
  clarification: PendingAskUserQuestion;
  onSubmit: (answers: AskUserQuestionSemanticAnswer[]) => Promise<void>;
  onTimeout: () => Promise<void>;
}

export default function RuntimeClarificationCard({
  clarification,
  onSubmit,
  onTimeout,
}: RuntimeClarificationCardProps) {
  const [drafts, setDrafts] = useState<Record<string, DraftAnswer>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeQuestionIndex, setActiveQuestionIndex] = useState(0);
  const [slideDirection, setSlideDirection] = useState<SlideDirection>('next');
  const [customValidation, setCustomValidation] = useState<Record<string, boolean>>({});
  const [countdown, setCountdown] = useState<number | null>(() => (
    remainingSeconds(clarification.expires_at)
  ));
  const timeoutSent = useRef(false);

  useEffect(() => {
    setDrafts({});
    setSubmitting(false);
    setError(null);
    setActiveQuestionIndex(0);
    setSlideDirection('next');
    setCustomValidation({});
    setCountdown(remainingSeconds(clarification.expires_at));
    timeoutSent.current = false;
  }, [clarification.expires_at, clarification.id]);

  useEffect(() => {
    if (!clarification.expires_at) return;
    const deadline = Date.parse(clarification.expires_at);
    if (Number.isNaN(deadline)) return;
    const notifyTimeout = () => {
      if (timeoutSent.current) return;
      timeoutSent.current = true;
      setSubmitting(true);
      void onTimeout().catch((timeoutError) => {
        timeoutSent.current = false;
        setError(timeoutError instanceof Error ? timeoutError.message : String(timeoutError));
        setSubmitting(false);
      });
    };
    let timer: number | undefined;
    const tick = () => {
      const milliseconds = deadline - Date.now();
      const seconds = Math.max(0, Math.ceil(milliseconds / 1000));
      setCountdown(seconds);
      if (milliseconds <= 0) {
        notifyTimeout();
        return;
      }
      timer = window.setTimeout(tick, Math.min(1000, milliseconds));
    };
    tick();
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [clarification.expires_at, onTimeout]);

  const validationState = useMemo(() => clarification.questions.map((question, index) => {
    const draft = drafts[String(index)];
    const answered = Boolean(draft?.selectedOptions.length || draft?.customSelected);
    const customValid = !draft?.customSelected || Boolean(draft.text.trim());
    return {
      answered,
      valid: customValid && (!question.required || answered),
    };
  }), [clarification.questions, drafts]);
  const complete = validationState.every((state) => state.valid);

  const activeQuestion = clarification.questions[activeQuestionIndex];
  const activeDraft = drafts[String(activeQuestionIndex)] ?? {
    selectedOptions: [],
    customSelected: false,
    text: '',
  };
  const activeCustomInvalid = Boolean(
    activeDraft.customSelected
    && customValidation[String(activeQuestionIndex)]
    && !activeDraft.text.trim(),
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
    const answers = clarification.questions.map<AskUserQuestionSemanticAnswer>((question, index) => {
      const draft = drafts[String(index)];
      return {
        question: question.question,
        selected_options: draft?.selectedOptions ?? [],
        custom_text: draft?.customSelected ? draft.text.trim() || null : null,
      };
    });
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(answers);
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
      setSubmitting(false);
    }
  };

  return (
    <section className={styles.clarificationDock} aria-label={clarification.title}>
      <header className={styles.clarificationHeader}>
        <div>
          <strong>{clarification.title}</strong>
          <span>回答后将在当前 Chat 中继续执行</span>
        </div>
        <span className={styles.clarificationStatus}>
          等待输入{countdown !== null ? ` · 剩余 ${formatCountdown(countdown)}` : ''}
        </span>
      </header>

      <div className={`${styles.clarificationCarousel} ${
        clarification.questions.length === 1 ? styles.clarificationCarouselSingle : ''
      }`}>
        {clarification.questions.length > 1 ? (
          <Button
            aria-label="上一题"
            className={`${styles.clarificationArrow} ${styles.clarificationArrowPrevious}`}
            disabled={activeQuestionIndex === 0}
            icon={<LeftOutlined />}
            type="text"
            onClick={() => navigateToQuestion(activeQuestionIndex - 1, 'previous')}
          />
        ) : null}

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
              {activeQuestion.required ? <em aria-label="必答">*</em> : null}
            </span>
            {clarification.questions.length > 1 ? (
              <small>{activeQuestionIndex + 1} / {clarification.questions.length}</small>
            ) : null}
          </div>
          {activeQuestion.description ? <p>{activeQuestion.description}</p> : null}
          {!activeQuestion.multiSelect ? (
            <Radio.Group
              className={styles.clarificationOptionGroup}
              value={activeDraft.customSelected
                ? CUSTOM_OPTION_VALUE
                : activeDraft.selectedOptions[0]}
              onChange={(event) => {
                const value = String(event.target.value);
                const customSelected = value === CUSTOM_OPTION_VALUE;
                updateDraft(activeQuestionIndex, {
                  selectedOptions: customSelected ? [] : [value],
                  customSelected,
                });
                clearCustomValidation(activeQuestionIndex);
                if (!customSelected && activeQuestionIndex < clarification.questions.length - 1) {
                  navigateToQuestion(activeQuestionIndex + 1, 'next');
                }
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
                  {option.description ? <small>{option.description}</small> : null}
                </Radio>
              ))}
              <Radio
                className={`${styles.clarificationOptionCard} ${
                  activeDraft.customSelected ? styles.clarificationOptionSelected : ''
                }`}
                value={CUSTOM_OPTION_VALUE}
              >
                <span>其他</span>
              </Radio>
            </Radio.Group>
          ) : null}
          {activeQuestion.multiSelect ? (
            <Checkbox.Group
              className={styles.clarificationOptionGroup}
              value={activeDraft.customSelected
                ? [...activeDraft.selectedOptions, CUSTOM_OPTION_VALUE]
                : activeDraft.selectedOptions}
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
                  {option.description ? <small>{option.description}</small> : null}
                </Checkbox>
              ))}
              <Checkbox
                className={`${styles.clarificationOptionCard} ${
                  activeDraft.customSelected ? styles.clarificationOptionSelected : ''
                }`}
                value={CUSTOM_OPTION_VALUE}
              >
                <span>其他</span>
              </Checkbox>
            </Checkbox.Group>
          ) : null}
          {activeDraft.customSelected ? (
            <div className={styles.clarificationCustomAnswer}>
              <Input.TextArea
                autoSize={{ minRows: 2, maxRows: 4 }}
                maxLength={1000}
                placeholder="请输入其他答案"
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
              {activeCustomInvalid ? <span role="alert">请输入其他答案</span> : null}
            </div>
          ) : null}
        </section>

        {clarification.questions.length > 1 ? (
          <Button
            aria-label="下一题"
            className={`${styles.clarificationArrow} ${styles.clarificationArrowNext}`}
            disabled={activeQuestionIndex === clarification.questions.length - 1}
            icon={<RightOutlined />}
            type="text"
            onClick={() => goToNextQuestion(activeQuestionIndex)}
          />
        ) : null}
      </div>

      <footer className={styles.clarificationFooter}>
        {error ? (
          <span className={styles.clarificationError} role="alert">
            {error}
          </span>
        ) : (
          <span />
        )}
        <Button
          className={styles.clarificationSubmit}
          type="primary"
          disabled={!complete}
          loading={submitting}
          onClick={() => void submit()}
        >
          提交并继续
        </Button>
      </footer>
    </section>
  );
}
