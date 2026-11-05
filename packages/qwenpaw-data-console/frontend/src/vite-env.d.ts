/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_AUTH_API_URL?: string;
  readonly VITE_AUTH_URL?: string;
  readonly VITE_AUTH_FRONTEND_URL?: string;
  readonly VITE_CONTEXT_FRONTEND_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
