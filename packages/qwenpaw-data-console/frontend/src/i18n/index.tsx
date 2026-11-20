/* eslint-disable react-refresh/only-export-components -- Provider, hook, and language contract are intentionally colocated. */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { enMessages, zhMessages, type MessageKey } from './locales';

export type SupportedLanguage = 'zh' | 'en';

export const LANGUAGE_STORAGE_KEY = 'language';

function normalizeLanguage(value?: string | null): SupportedLanguage {
  return value?.toLowerCase().split('-')[0] === 'en' ? 'en' : 'zh';
}

function detectLanguage(): SupportedLanguage {
  if (typeof window === 'undefined') return 'zh';
  return normalizeLanguage(window.localStorage.getItem(LANGUAGE_STORAGE_KEY));
}

type TranslationParams = Record<string, string | number>;

function formatMessage(template: string, params?: TranslationParams): string {
  if (!params) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (placeholder, key: string) =>
    Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : placeholder,
  );
}

interface AppI18nValue {
  language: SupportedLanguage;
  changeLanguage: (language: SupportedLanguage) => void;
  toggleLanguage: () => void;
  t: (key: MessageKey, params?: TranslationParams) => string;
}

const AppI18nContext = createContext<AppI18nValue | null>(null);

export function AppI18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<SupportedLanguage>(detectLanguage);

  const changeLanguage = useCallback((nextLanguage: SupportedLanguage) => {
    window.localStorage.setItem(LANGUAGE_STORAGE_KEY, nextLanguage);
    setLanguage(nextLanguage);
  }, []);

  const toggleLanguage = useCallback(() => {
    setLanguage((currentLanguage) => {
      const nextLanguage = currentLanguage === 'zh' ? 'en' : 'zh';
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, nextLanguage);
      return nextLanguage;
    });
  }, []);

  useEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
  }, [language]);

  const value = useMemo<AppI18nValue>(
    () => ({
      language,
      changeLanguage,
      toggleLanguage,
      t: (key, params) =>
        formatMessage(language === 'zh' ? zhMessages[key] : enMessages[key], params),
    }),
    [changeLanguage, language, toggleLanguage],
  );

  return <AppI18nContext.Provider value={value}>{children}</AppI18nContext.Provider>;
}

export function useAppI18n(): AppI18nValue {
  const value = useContext(AppI18nContext);
  if (!value) {
    throw new Error('useAppI18n must be used within AppI18nProvider');
  }
  return value;
}
