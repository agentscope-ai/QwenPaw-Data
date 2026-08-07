import { Button, GlobalOutlined, Tooltip } from "@/design";
import { useAppI18n } from "@/i18n/useAppI18n";

export default function LanguageSwitcher() {
  const { language, changeLanguage, t } = useAppI18n();
  const nextLanguage = language === "zh" ? "en" : "zh";
  const label = t(
    nextLanguage === "zh"
      ? "common.switchLanguageToChinese"
      : "common.switchLanguageToEnglish",
  );

  return (
    <Tooltip title={label}>
      <Button
        type="text"
        aria-label={t("common.language")}
        icon={<GlobalOutlined />}
        onClick={() => changeLanguage(nextLanguage)}
      />
    </Tooltip>
  );
}
