import { useEffect, useMemo, useState } from "react";
import { Row, Col } from "@/design";
import { Button, Form, Input, Select } from "@/design";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { ROUTES } from "@/router";
import {
  httpDataSourceApi,
  type DataSourceConnectionConfig,
  type DataSourceCreatePayload,
  type DataSourceType,
} from "@/services/dataConnection";
import { useAppMessage } from "@/hooks/useAppMessage";
import { DATA_CONNECTION_TYPE_META } from "../helpers/types";
import { useDataSourceTypes } from "../hooks/useDataSourceTypes";
import {
  formatCreateSuccessMessage,
  formatTestSuccessMessage,
  resolveApiErrorCode,
  resolveErrorMessage,
} from "../helpers/errors";
import styles from "./AddDataSourcePage.module.less";

interface AddFormValues {
  type: DataSourceType;
  name: string;
  host?: string;
  port?: number | string;
  user?: string;
  password?: string;
  db?: string;
  endpoint?: string;
  project_name?: string;
  access_id?: string;
  access_key?: string;
  app_name?: string;
}

/** Convert Ant Design form values into the backend create-data-source payload. */
function toPayload(values: AddFormValues): DataSourceCreatePayload {
  const name = values.name.trim();
  if (values.type === "odps") {
    const config: DataSourceConnectionConfig = {
      endpoint: values.endpoint?.trim(),
      project_name: values.project_name?.trim(),
      access_id: values.access_id?.trim(),
      access_key: values.access_key,
      app_name: values.app_name?.trim(),
    };
    return { type: values.type, name, config };
  }

  const port =
    values.port === undefined || values.port === null || values.port === ""
      ? undefined
      : Number(values.port);
  const config: DataSourceConnectionConfig = {
    host: values.host?.trim(),
    port: Number.isFinite(port) ? port : undefined,
    user: values.user?.trim(),
    password: values.password,
    db: values.db?.trim(),
  };
  return { type: values.type, name, config };
}

/** Add Data Source form content; provider wrapping is kept in the default export. */
function AddDataSourcePageInner() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const navigate = useNavigate();
  const [form] = Form.useForm<AddFormValues>();
  const [testing, setTesting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const { types, loading: typesLoading } = useDataSourceTypes();

  const selectedType = Form.useWatch("type", form);

  useEffect(() => {
    if (types.length === 0) return;
    const currentType = form.getFieldValue("type");
    if (!currentType || !types.some((item) => item.type === currentType)) {
      const first = types[0];
      form.setFieldsValue({
        type: first.type,
        port: first.defaultPort,
      });
    }
  }, [types, form]);

  const typeOptions = useMemo(
    () =>
      types.map((item) => ({
        value: item.type,
        label: t(
          DATA_CONNECTION_TYPE_META[item.type]?.labelKey ??
            `dataConnection.types.${item.type}`,
        ),
      })),
    [t, types],
  );

  /** Validate current form values and test the connection without saving it. */
  const handleTest = async () => {
    try {
      const values = await form.validateFields();
      setTesting(true);
      const payload = toPayload(values);
      const result = await httpDataSourceApi.testConnection({
        type: payload.type,
        config: payload.config,
      });
      if (result.success) {
        message.success(formatTestSuccessMessage(t, result));
      } else {
        message.error(
          resolveErrorMessage(
            t,
            result.message,
            "dataConnection.errors.testFailed",
          ),
        );
      }
    } catch (error) {
      if (error instanceof Error && error.message !== "Not authenticated") {
        message.error(
          resolveErrorMessage(
            t,
            resolveApiErrorCode(error, "testFailed"),
            "dataConnection.errors.testFailed",
          ),
        );
      }
    } finally {
      setTesting(false);
    }
  };

  /** Create the data source and return to the list page on success. */
  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      const payload = toPayload(values);
      const record = await httpDataSourceApi.create(payload);
      message.success(
        formatCreateSuccessMessage(t, {
          ...record,
          name: record.name || payload.name,
        }),
      );
      navigate(ROUTES.DATA_CONNECTION);
    } catch (error) {
      message.error(
        resolveErrorMessage(
          t,
          resolveApiErrorCode(error, "createFailed"),
          "dataConnection.errors.createFailed",
        ),
      );
    } finally {
      setSubmitting(false);
    }
  };

  const isOdps = selectedType === "odps";

  return (
    <div className={styles.addPage}>
      <div className={styles.formPanel}>
        <h2 className={styles.formTitle}>
          {t("dataConnection.addModalTitle")}
        </h2>

        <Form form={form} layout="vertical">
          <Form.Item
            name="type"
            label={t("dataConnection.type")}
            rules={[
              { required: true, message: t("dataConnection.typeRequired") },
            ]}
          >
            <Select
              loading={typesLoading}
              options={typeOptions}
              placeholder={t("dataConnection.typePlaceholder")}
            />
          </Form.Item>

          <Form.Item
            name="name"
            label={t("dataConnection.name")}
            rules={[
              { required: true, message: t("dataConnection.nameRequired") },
            ]}
          >
            <Input placeholder={t("dataConnection.namePlaceholder")} />
          </Form.Item>

          {isOdps ? (
            <>
              <Form.Item
                name="endpoint"
                label={t("dataConnection.endpoint")}
                rules={[
                  {
                    required: true,
                    message: t("dataConnection.endpointRequired"),
                  },
                ]}
              >
                <Input placeholder={t("dataConnection.endpointPlaceholder")} />
              </Form.Item>

              <Form.Item
                name="project_name"
                label={t("dataConnection.projectName")}
                rules={[
                  {
                    required: true,
                    message: t("dataConnection.projectNameRequired"),
                  },
                ]}
              >
                <Input
                  placeholder={t("dataConnection.projectNamePlaceholder")}
                />
              </Form.Item>

              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="access_id"
                    label={t("dataConnection.accessId")}
                    rules={[
                      {
                        required: true,
                        message: t("dataConnection.accessIdRequired"),
                      },
                    ]}
                  >
                    <Input
                      placeholder={t("dataConnection.accessIdPlaceholder")}
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="access_key"
                    label={t("dataConnection.accessKey")}
                    rules={[
                      {
                        required: true,
                        message: t("dataConnection.accessKeyRequired"),
                      },
                    ]}
                  >
                    <Input.Password
                      placeholder={t("dataConnection.accessKeyPlaceholder")}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item
                name="app_name"
                label={t("dataConnection.appName")}
                rules={[
                  {
                    required: true,
                    message: t("dataConnection.appNameRequired"),
                  },
                ]}
              >
                <Input placeholder={t("dataConnection.appNamePlaceholder")} />
              </Form.Item>
            </>
          ) : (
            <>
              <Row gutter={16}>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="host"
                    label={t("dataConnection.host")}
                    rules={[
                      {
                        required: true,
                        message: t("dataConnection.hostRequired"),
                      },
                    ]}
                  >
                    <Input placeholder={t("dataConnection.hostPlaceholder")} />
                  </Form.Item>
                </Col>
                <Col xs={24} md={12}>
                  <Form.Item
                    name="port"
                    label={t("dataConnection.port")}
                    rules={[
                      {
                        required: true,
                        message: t("dataConnection.portRequired"),
                      },
                    ]}
                  >
                    <Input
                      type="number"
                      placeholder={t("dataConnection.portPlaceholder")}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    name="user"
                    label={t("dataConnection.user")}
                    rules={[
                      {
                        required: true,
                        message: t("dataConnection.userRequired"),
                      },
                    ]}
                  >
                    <Input placeholder={t("dataConnection.userPlaceholder")} />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    name="password"
                    label={t("dataConnection.password")}
                    rules={[
                      {
                        required: true,
                        message: t("dataConnection.passwordRequired"),
                      },
                    ]}
                  >
                    <Input.Password
                      placeholder={t("dataConnection.passwordPlaceholder")}
                    />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item
                name="db"
                label={t("dataConnection.db")}
                rules={[
                  { required: true, message: t("dataConnection.dbRequired") },
                ]}
              >
                <Input placeholder={t("dataConnection.dbPlaceholder")} />
              </Form.Item>
            </>
          )}

          <div className={styles.actions}>
            <Button loading={testing} onClick={() => void handleTest()}>
              {t("dataConnection.testConnection")}
            </Button>
            <Button
              type="primary"
              loading={submitting}
              onClick={() => void handleSubmit()}
            >
              {t("common.confirm")}
            </Button>
            <Button onClick={() => navigate(ROUTES.DATA_CONNECTION)}>
              {t("common.cancel")}
            </Button>
          </div>
        </Form>
      </div>
    </div>
  );
}

/** Data Source creation page. */
export default function AddDataSourcePage() {
  return <AddDataSourcePageInner />;
}
