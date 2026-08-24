import { Alert, Button, Progress, Space, Typography } from "@/design";
import { useTranslation } from "react-i18next";
import type { RebuildJob } from "@/services/modelConfig";
import styles from "../index.module.less";

interface RebuildStatusProps {
  job: RebuildJob | null;
  retrying: boolean;
  pollError: string | null;
  onRetry: () => void;
}

export default function RebuildStatus({
  job,
  retrying,
  pollError,
  onRetry,
}: RebuildStatusProps) {
  const { t } = useTranslation();

  if (!job && !pollError) return null;

  const total = job?.progress.labels_total ?? 0;
  const done = job?.progress.labels_done ?? 0;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const status = job?.status;
  const type =
    status === "success" ? "success" : status === "failed" ? "error" : "info";
  const title = status ? t(`modelConfig.rebuild.${status}Title`) : undefined;

  return (
    <Space className={styles.rebuildStatus} direction="vertical" size={12}>
      {job ? (
        <Alert
          type={type}
          showIcon
          message={title}
          description={
            <Space direction="vertical" size={4}>
              {job.progress.phase ? (
                <Typography.Text>
                  {t("modelConfig.rebuild.phase", { value: job.progress.phase })}
                </Typography.Text>
              ) : null}
              {job.progress.current_label ? (
                <Typography.Text>
                  {t("modelConfig.rebuild.currentLabel", {
                    value: job.progress.current_label,
                  })}
                </Typography.Text>
              ) : null}
              {job.error ? <Typography.Text type="danger">{job.error}</Typography.Text> : null}
            </Space>
          }
          action={
            status === "failed" ? (
              <Button danger loading={retrying} onClick={onRetry}>
                {t("modelConfig.rebuild.retry")}
              </Button>
            ) : undefined
          }
        />
      ) : null}

      {status === "pending" || status === "running" ? (
        <Progress
          percent={percent}
          status="active"
          format={() => t("modelConfig.rebuild.progress", { done, total })}
        />
      ) : null}

      {pollError ? (
        <Alert
          type="warning"
          showIcon
          message={t("modelConfig.rebuild.pollFailed")}
          description={pollError}
        />
      ) : null}
    </Space>
  );
}
