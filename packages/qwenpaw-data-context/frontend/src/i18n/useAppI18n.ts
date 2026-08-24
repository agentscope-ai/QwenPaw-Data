import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import i18n, {
  LANGUAGE_STORAGE_KEY,
  languageOptions,
  normalizeLang,
  type SupportedLang,
} from "./index";

export function setAppLanguage(language: SupportedLang) {
  localStorage.setItem(LANGUAGE_STORAGE_KEY, language);
  return i18n.changeLanguage(language);
}

export function useAppI18n() {
  const { t, i18n: i18nInstance } = useTranslation();
  const language = normalizeLang(i18nInstance.language);

  const changeLanguage = useCallback((nextLanguage: SupportedLang) => {
    void setAppLanguage(nextLanguage);
  }, []);

  return {
    t,
    language,
    languageOptions,
    changeLanguage,
  };
}
