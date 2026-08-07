import { Button, SettingOutlined, Tooltip } from "@/design";
import { useAppI18n } from "@/i18n/useAppI18n";
import { useNavigate } from "react-router";

const MODEL_CONFIG_PATH = "/model-config";

export default function ModelConfigButton() {
  const navigate = useNavigate();
  const { t } = useAppI18n();
  const label = t("menu.modelConfig");

  return (
    <Tooltip title={label}>
      <Button
        type="text"
        aria-label={label}
        icon={<SettingOutlined />}
        onClick={() => navigate(MODEL_CONFIG_PATH)}
      />
    </Tooltip>
  );
}
