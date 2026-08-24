import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import en from "./locales/en.json";
import zh from "./locales/zh.json";

export type SupportedLang = "en" | "zh";

export const LANGUAGE_STORAGE_KEY = "language";

export const languageOptions: { label: string; value: SupportedLang }[] = [
  { label: "中文", value: "zh" },
  { label: "English", value: "en" },
];

/** Normalize browser or stored locale strings to one of the supported bundles. */
export function normalizeLang(locale?: string | null): SupportedLang {
  const lang = (locale || "en").toLowerCase().split("-")[0];
  if (lang === "zh") return "zh";
  return "en";
}

/** Detect the preferred UI language from local storage or the browser locale. */
export function detectLang(): SupportedLang {
  const stored = localStorage.getItem(LANGUAGE_STORAGE_KEY);
  return normalizeLang(stored || navigator.language || "en");
}


const resources = {
  en: {
    translation: en,
  },
  zh: {
    translation: zh,
  },
};

i18n.use(initReactI18next).init({
  resources,
  lng: detectLang(),
  fallbackLng: "en",
  interpolation: {
    escapeValue: false,
  },
});

export default i18n;
