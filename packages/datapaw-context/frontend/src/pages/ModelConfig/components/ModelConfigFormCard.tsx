import type { ReactNode } from "react";
import { Card } from "antd";
import { Spin } from "@/design";
import styles from "../index.module.less";

interface ModelConfigFormCardProps {
  title: ReactNode;
  actions: ReactNode;
  children: ReactNode;
  status?: ReactNode;
  loading?: boolean;
}

export default function ModelConfigFormCard({
  title,
  actions,
  children,
  status,
  loading = false,
}: ModelConfigFormCardProps) {
  return (
    <Card className={styles.configCard} title={title} extra={actions}>
      <Spin spinning={loading}>
        <div className={styles.formContent}>{children}</div>
        {status ? <div className={styles.statusArea}>{status}</div> : null}
      </Spin>
    </Card>
  );
}
