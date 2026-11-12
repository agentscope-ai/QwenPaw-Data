import { useCallback, useLayoutEffect, useState } from 'react';

export type QwenPawDataColorMode = 'light' | 'dark';

const COLOR_MODE_STORAGE_KEY = 'qwenpaw-data-color-mode';
const COLOR_MODE_EVENT = 'qwenpaw-data-color-mode-change';

function isColorMode(value: unknown): value is QwenPawDataColorMode {
  return value === 'light' || value === 'dark';
}

function readStoredColorMode(): QwenPawDataColorMode {
  if (typeof window === 'undefined') return 'light';

  try {
    const storedMode = window.localStorage.getItem(COLOR_MODE_STORAGE_KEY);
    return isColorMode(storedMode) ? storedMode : 'light';
  } catch {
    return 'light';
  }
}

function applyColorMode(colorMode: QwenPawDataColorMode, persist: boolean) {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.qwenpawDataTheme = colorMode;
    document.documentElement.style.colorScheme = colorMode;
  }

  if (!persist || typeof window === 'undefined') return;

  try {
    window.localStorage.setItem(COLOR_MODE_STORAGE_KEY, colorMode);
  } catch {
    // Theme changes still work when storage is unavailable (for example in private mode).
  }
  window.dispatchEvent(new CustomEvent<QwenPawDataColorMode>(COLOR_MODE_EVENT, {
    detail: colorMode,
  }));
}

export function useQwenPawDataColorMode() {
  const [colorMode, setColorModeState] = useState<QwenPawDataColorMode>(readStoredColorMode);

  useLayoutEffect(() => {
    applyColorMode(colorMode, false);

    const handleColorModeChange = (event: Event) => {
      const nextMode = (event as CustomEvent<QwenPawDataColorMode>).detail;
      if (isColorMode(nextMode)) setColorModeState(nextMode);
    };
    const handleStorage = (event: StorageEvent) => {
      if (event.key === COLOR_MODE_STORAGE_KEY && isColorMode(event.newValue)) {
        setColorModeState(event.newValue);
      }
    };

    window.addEventListener(COLOR_MODE_EVENT, handleColorModeChange);
    window.addEventListener('storage', handleStorage);
    return () => {
      window.removeEventListener(COLOR_MODE_EVENT, handleColorModeChange);
      window.removeEventListener('storage', handleStorage);
    };
  }, [colorMode]);

  const setColorMode = useCallback((nextMode: QwenPawDataColorMode) => {
    setColorModeState(nextMode);
    applyColorMode(nextMode, true);
  }, []);

  const toggleColorMode = useCallback(() => {
    setColorMode(colorMode === 'light' ? 'dark' : 'light');
  }, [colorMode, setColorMode]);

  return { colorMode, setColorMode, toggleColorMode } as const;
}
