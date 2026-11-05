function getEnv(): Record<string, string | boolean | undefined> | undefined {
  return (import.meta as unknown as {
    env?: Record<string, string | boolean | undefined>;
  }).env;
}

function stringValue(value: string | boolean | undefined): string {
  return typeof value === "string" ? value : "";
}

function getEnvApiBaseUrl(): string {
  return stringValue(getEnv()?.VITE_API_BASE_URL);
}

function getEnvAuthApiBaseUrl(): string {
  const env = getEnv();
  if (env?.DEV === true) return "";
  return stringValue(env?.VITE_AUTH_API_URL) || stringValue(env?.VITE_AUTH_URL);
}

export function getApiUrl(path: string): string {
  const base = getEnvApiBaseUrl().replace(/\/$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const prefixedPath = normalizedPath.startsWith("/api/v1/")
    ? normalizedPath
    : `/api/v1${normalizedPath}`;
  return `${base}${prefixedPath}`;
}

export function getContextFrontendUrl(path = ""): string {
  const base = (stringValue(getEnv()?.VITE_CONTEXT_FRONTEND_URL) || "/databridge")
    .replace(/\/$/, "");
  const normalizedPath = path
    ? (path.startsWith("/") ? path : `/${path}`)
    : "";
  return `${base}${normalizedPath}`;
}

export function getAuthApiUrl(path: string): string {
  const base = getEnvAuthApiBaseUrl().replace(/\/$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const prefixedPath = normalizedPath.startsWith("/api/auth/")
    ? normalizedPath
    : `/api/auth${normalizedPath}`;
  return `${base}${prefixedPath}`;
}

export function getAuthLoginUrl(redirectUrl: string): string {
  const env = getEnv();
  const base = (
    stringValue(env?.VITE_AUTH_FRONTEND_URL)
    || stringValue(env?.VITE_CONTEXT_FRONTEND_URL)
    || "http://localhost:3000"
  ).replace(/\/$/, "");
  return `${base}/login?redirect=${encodeURIComponent(redirectUrl)}`;
}
