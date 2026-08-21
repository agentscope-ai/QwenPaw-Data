import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Alert,
  ApiOutlined,
  Button,
  DeploymentUnitOutlined,
  Form,
  Input,
  InputNumber,
  Radio,
  Space,
} from "@/design";
import { useAppMessage } from "@/hooks/useAppMessage";
import {
  clearModelConfigEntryCheck,
  modelConfigApi,
  readModelConfigEntryCheck,
  type EmbeddingConfig,
  type EmbeddingConfigPayload,
  type LLMConfig,
  type LLMConfigPayload,
  type TestResult,
} from "@/services/modelConfig";
import ModelConfigFormCard from "./components/ModelConfigFormCard";
import RebuildStatus from "./components/RebuildStatus";
import {
  getProviderBaseUrl,
  inferProvider,
  LLM_PROVIDER_PRESETS,
  type LLMProvider,
} from "./providerPresets";
import { useEmbeddingRebuild } from "./useEmbeddingRebuild";
import styles from "./index.module.less";

interface LLMFormValues {
  provider: LLMProvider;
  base_url: string;
  model: string;
  api_key?: string;
}

interface EmbeddingFormValues {
  base_url: string;
  model: string;
  dim: number;
  api_key?: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function configuredKeyMask(maskedKey: string | null | undefined): string {
  return maskedKey?.trim() ?? "";
}

function entryCheckResult(
  result: TestResult,
  complete: boolean,
  incompleteMessage: string,
): TestResult {
  if (complete) return result;
  return {
    ...result,
    success: false,
    message: result.success
      ? incompleteMessage
      : `${incompleteMessage} ${result.message}`,
  };
}

export default function ModelConfigPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [entryCheck] = useState(() => readModelConfigEntryCheck());
  const [llmForm] = Form.useForm<LLMFormValues>();
  const [embeddingForm] = Form.useForm<EmbeddingFormValues>();
  const [initialLoading, setInitialLoading] = useState(true);
  const [configLoaded, setConfigLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [llmTesting, setLlmTesting] = useState(false);
  const [llmSaving, setLlmSaving] = useState(false);
  const [embeddingTesting, setEmbeddingTesting] = useState(false);
  const [embeddingSaving, setEmbeddingSaving] = useState(false);
  const [llmTestResult, setLlmTestResult] = useState<TestResult | null>(() =>
    entryCheck
      ? entryCheckResult(
          entryCheck.llm,
          entryCheck.llmComplete,
          t("modelConfig.llmIncomplete"),
        )
      : null,
  );
  const [embeddingTestResult, setEmbeddingTestResult] =
    useState<TestResult | null>(() =>
      entryCheck
        ? entryCheckResult(
            entryCheck.embedding,
            entryCheck.embeddingComplete,
            t("modelConfig.embeddingIncomplete"),
          )
        : null,
    );
  const [llmKeyMask, setLlmKeyMask] = useState("");
  const [embeddingKeyMask, setEmbeddingKeyMask] = useState("");
  const [missingRebuildJob, setMissingRebuildJob] = useState(false);
  const llmAuxModelRef = useRef<string | null>(null);

  const applyLLMConfig = useCallback(
    (llm: LLMConfig) => {
      llmAuxModelRef.current = llm.aux_model ?? null;
      setLlmKeyMask(configuredKeyMask(llm.api_key));
      llmForm.setFieldsValue({
        provider: inferProvider(llm.base_url, LLM_PROVIDER_PRESETS, "custom"),
        base_url: llm.base_url ?? "",
        model: llm.model ?? "",
        api_key: "",
      });
    },
    [llmForm],
  );

  const applyEmbeddingConfig = useCallback(
    (embedding: EmbeddingConfig) => {
      setEmbeddingKeyMask(configuredKeyMask(embedding.api_key));
      embeddingForm.setFieldsValue({
        base_url: embedding.base_url ?? "",
        model: embedding.model ?? "",
        dim: embedding.dim ?? 1024,
        api_key: "",
      });
    },
    [embeddingForm],
  );

  const handleRebuildSuccess = useCallback(() => {
    message.success(t("modelConfig.rebuild.successToast"));
    void modelConfigApi
      .get()
      .then(({ embedding }) => applyEmbeddingConfig(embedding))
      .catch((error) => {
        message.error(
          t("modelConfig.rebuild.refreshFailed", {
            message: errorMessage(error),
          }),
        );
      });
  }, [applyEmbeddingConfig, message, t]);

  const {
    job,
    active: rebuildActive,
    retrying,
    pollError,
    restoreJob,
    beginJob,
    retry,
  } = useEmbeddingRebuild({ onSuccess: handleRebuildSuccess });

  const loadInitialData = useCallback(async () => {
    setInitialLoading(true);
    setConfigLoaded(false);
    setLoadError(null);

    const [configResult, jobResult] = await Promise.allSettled([
      modelConfigApi.get(),
      modelConfigApi.getLatestRebuildJob(),
    ]);
    const errors: string[] = [];

    if (configResult.status === "fulfilled") {
      const { llm, embedding } = configResult.value;
      applyLLMConfig(llm);
      applyEmbeddingConfig(embedding);
      setConfigLoaded(true);
    } else {
      errors.push(errorMessage(configResult.reason));
    }

    if (jobResult.status === "fulfilled") {
      restoreJob(jobResult.value);
    } else {
      errors.push(errorMessage(jobResult.reason));
    }

    if (errors.length > 0) {
      setLoadError(errors.join("; "));
    }
    setInitialLoading(false);
  }, [applyEmbeddingConfig, applyLLMConfig, restoreJob]);

  useEffect(() => {
    if (entryCheck) {
      clearModelConfigEntryCheck();
    }
  }, [entryCheck]);

  useEffect(() => {
    void loadInitialData();
  }, [loadInitialData]);

  const buildLLMPayload = (values: LLMFormValues): LLMConfigPayload => ({
    base_url: values.base_url?.trim() ?? "",
    model: values.model?.trim() ?? "",
    aux_model: llmAuxModelRef.current,
    api_key: values.api_key?.trim() ?? "",
  });

  const buildEmbeddingPayload = (
    values: EmbeddingFormValues,
  ): EmbeddingConfigPayload => ({
    base_url: values.base_url?.trim() ?? "",
    model: values.model?.trim() ?? "",
    dim: values.dim,
    api_key: values.api_key?.trim() ?? "",
  });

  const handleLLMProviderChange = (provider: LLMProvider) => {
    llmForm.setFieldValue(
      "base_url",
      getProviderBaseUrl(provider, LLM_PROVIDER_PRESETS),
    );
    setLlmTestResult(null);
  };

  const handleTestLLM = async () => {
    try {
      const values = await llmForm.validateFields();
      setLlmTesting(true);
      const result = await modelConfigApi.testLLM(buildLLMPayload(values));
      setLlmTestResult(result);
    } catch (error) {
      if (error instanceof Error) {
        message.error(t("modelConfig.testRequestFailed", { message: error.message }));
      }
    } finally {
      setLlmTesting(false);
    }
  };

  const handleSaveLLM = async () => {
    try {
      const values = await llmForm.validateFields();
      setLlmSaving(true);
      const result = await modelConfigApi.updateLLM(buildLLMPayload(values));
      applyLLMConfig(result.llm);
      setLlmTestResult(null);
      message.success(t("modelConfig.llmSaveSuccess"));
    } catch (error) {
      if (error instanceof Error) {
        message.error(t("modelConfig.saveFailed", { message: error.message }));
      }
    } finally {
      setLlmSaving(false);
    }
  };

  const handleTestEmbedding = async () => {
    try {
      const values = await embeddingForm.validateFields();
      setEmbeddingTesting(true);
      const result = await modelConfigApi.testEmbedding(buildEmbeddingPayload(values));
      setEmbeddingTestResult(result);
      if (result.success && result.detected_dim) {
        embeddingForm.setFieldValue("dim", result.detected_dim);
      }
    } catch (error) {
      if (error instanceof Error) {
        message.error(t("modelConfig.testRequestFailed", { message: error.message }));
      }
    } finally {
      setEmbeddingTesting(false);
    }
  };

  const handleSaveEmbedding = async () => {
    try {
      const values = await embeddingForm.validateFields();
      setEmbeddingSaving(true);
      const result = await modelConfigApi.updateEmbedding(
        buildEmbeddingPayload(values),
      );
      const embedding = result.embedding;
      applyEmbeddingConfig({ ...embedding, dim: embedding.dim ?? values.dim });
      setEmbeddingTestResult(null);
      setMissingRebuildJob(false);

      if (result.rebuild_required) {
        if (result.job_id) {
          beginJob(result.job_id);
        } else {
          setMissingRebuildJob(true);
        }
      }
      message.success(t("modelConfig.embeddingSaveSuccess"));
    } catch (error) {
      if (error instanceof Error) {
        message.error(t("modelConfig.saveFailed", { message: error.message }));
      }
    } finally {
      setEmbeddingSaving(false);
    }
  };

  const renderProviderButtons = <T extends string>(
    presets: readonly { value: T; label: string }[],
  ) =>
    presets.map((preset) => (
      <Radio.Button key={preset.value} value={preset.value}>
        {preset.label}
      </Radio.Button>
    ));

  const llmStatus = llmTestResult ? (
    <Alert
      showIcon
      type={llmTestResult.success ? "success" : "error"}
      message={llmTestResult.message}
    />
  ) : null;
  const hasEmbeddingStatus = Boolean(
    embeddingTestResult || missingRebuildJob || job || pollError,
  );
  const embeddingStatus = hasEmbeddingStatus ? (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {embeddingTestResult ? (
        <Alert
          showIcon
          type={embeddingTestResult.success ? "success" : "error"}
          message={embeddingTestResult.message}
        />
      ) : null}
      {missingRebuildJob ? (
        <Alert
          showIcon
          type="warning"
          message={t("modelConfig.rebuild.missingJob")}
        />
      ) : null}
      <RebuildStatus
        job={job}
        retrying={retrying}
        pollError={pollError}
        onRetry={() => void retry()}
      />
    </Space>
  ) : null;

  return (
    <div className={styles.page}>
      {loadError ? (
        <Alert
          className={styles.loadError}
          type="error"
          showIcon
          message={t("modelConfig.loadFailed")}
          description={loadError}
          action={<Button onClick={() => void loadInitialData()}>{t("common.retry")}</Button>}
        />
      ) : null}

      <div className={styles.grid}>
        <ModelConfigFormCard
          loading={initialLoading}
          title={
            <span className={styles.cardTitle}>
              <ApiOutlined className={styles.llmIcon} />
              {t("modelConfig.llmTitle")}
            </span>
          }
          actions={
            <div className={styles.actions}>
              <Button
                disabled={!configLoaded}
                loading={llmTesting}
                onClick={() => void handleTestLLM()}
              >
                {t("modelConfig.testConnection")}
              </Button>
              <Button
                type="primary"
                disabled={!configLoaded}
                loading={llmSaving}
                onClick={() => void handleSaveLLM()}
              >
                {t("common.save")}
              </Button>
            </div>
          }
          status={llmStatus}
        >
          <Form
            form={llmForm}
            layout="vertical"
            disabled={!configLoaded}
            initialValues={{ provider: "dashscope", base_url: "", model: "" }}
          >
            <Form.Item name="provider" label={t("modelConfig.provider")}>
              <Radio.Group
                className={styles.providerGroup}
                buttonStyle="solid"
                onChange={(event) =>
                  handleLLMProviderChange(event.target.value as LLMProvider)
                }
              >
                {renderProviderButtons(LLM_PROVIDER_PRESETS)}
              </Radio.Group>
            </Form.Item>
            <Form.Item name="base_url" label={t("modelConfig.endpoint")}>
              <Input placeholder={t("modelConfig.endpointPlaceholder")} allowClear />
            </Form.Item>
            <Form.Item
              name="model"
              label={t("modelConfig.model")}
              rules={[{ required: true, message: t("modelConfig.modelRequired") }]}
            >
              <Input placeholder={t("modelConfig.modelPlaceholder")} allowClear />
            </Form.Item>
            <Form.Item
              name="api_key"
              label={t("modelConfig.apiKey")}
              extra={
                llmKeyMask ? (
                  <span className={styles.keyHint}>
                    {t("modelConfig.apiKeyConfigured", { masked: llmKeyMask })}
                  </span>
                ) : undefined
              }
            >
              <Input.Password
                autoComplete="new-password"
                placeholder={t("modelConfig.apiKeyPlaceholder")}
              />
            </Form.Item>
          </Form>
        </ModelConfigFormCard>

        <ModelConfigFormCard
          loading={initialLoading}
          title={
            <span className={styles.cardTitle}>
              <DeploymentUnitOutlined className={styles.embeddingIcon} />
              {t("modelConfig.embeddingTitle")}
            </span>
          }
          actions={
            <div className={styles.actions}>
              <Button
                disabled={!configLoaded || rebuildActive}
                loading={embeddingTesting}
                onClick={() => void handleTestEmbedding()}
              >
                {t("modelConfig.testConnection")}
              </Button>
              <Button
                type="primary"
                disabled={!configLoaded || rebuildActive}
                loading={embeddingSaving}
                onClick={() => void handleSaveEmbedding()}
              >
                {t("common.save")}
              </Button>
            </div>
          }
          status={embeddingStatus}
        >
          <Form
            form={embeddingForm}
            layout="vertical"
            disabled={!configLoaded || rebuildActive}
            initialValues={{
              base_url: "",
              model: "",
              dim: 1024,
            }}
          >
            <Alert
              className={styles.rebuildHint}
              type="info"
              showIcon
              message={t("modelConfig.rebuildImpact")}
            />
            <Form.Item name="base_url" label={t("modelConfig.baseUrl")}>
              <Input placeholder={t("modelConfig.embeddingBaseUrlPlaceholder")} allowClear />
            </Form.Item>
            <Form.Item
              name="model"
              label={t("modelConfig.model")}
              rules={[{ required: true, message: t("modelConfig.modelRequired") }]}
            >
              <Input placeholder={t("modelConfig.modelPlaceholder")} allowClear />
            </Form.Item>
            <Form.Item
              name="dim"
              label={t("modelConfig.dimension")}
              extra={t("modelConfig.dimensionHint")}
              rules={[{ required: true, message: t("modelConfig.dimensionRequired") }]}
            >
              <InputNumber min={1} precision={0} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item
              name="api_key"
              label={t("modelConfig.apiKey")}
              extra={
                embeddingKeyMask ? (
                  <span className={styles.keyHint}>
                    {t("modelConfig.apiKeyConfigured", {
                      masked: embeddingKeyMask,
                    })}
                  </span>
                ) : undefined
              }
            >
              <Input.Password
                autoComplete="new-password"
                placeholder={t("modelConfig.apiKeyPlaceholder")}
              />
            </Form.Item>
          </Form>
        </ModelConfigFormCard>
      </div>
    </div>
  );
}
