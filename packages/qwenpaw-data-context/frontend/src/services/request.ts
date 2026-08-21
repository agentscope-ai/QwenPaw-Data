import { buildAuthHeaders } from "./authHeaders";
import { clearAuthToken, getApiUrl } from "./config";

function getErrorMessageFromBody(
  text: string,
  contentType: string,
): string | null {
  if (!text) return null;
  if (!contentType.includes("application/json")) return text;

  try {
    const payload = JSON.parse(text) as {
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    };
    if (typeof payload.detail === "string" && payload.detail) return payload.detail;
    if (typeof payload.message === "string" && payload.message) return payload.message;
    if (typeof payload.error === "string" && payload.error) return payload.error;
  } catch {
    return text;
  }

  return text;
}

function buildHeaders(method?: string, extra?: HeadersInit): Headers {
  const headers = extra instanceof Headers ? extra : new Headers(extra);

  if (method && ["POST", "PUT", "PATCH"].includes(method.toUpperCase())) {
    if (!headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
  }

  for (const [key, value] of Object.entries(buildAuthHeaders())) {
    if (!headers.has(key)) {
      headers.set(key, value);
    }
  }

  return headers;
}

export async function request<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(getApiUrl(path), {
    ...options,
    headers: buildHeaders(options.method || "GET", options.headers),
  });

  if (!response.ok) {
    if (response.status === 401) {
      clearAuthToken();
    }

    const text = await response.text().catch(() => "");
    const contentType = response.headers.get("content-type") || "";
    const errorMessage = getErrorMessageFromBody(text, contentType);
    throw new Error(
      errorMessage
        ? `${errorMessage} - ${text}`
        : `Request failed: ${response.status} ${response.statusText}`,
    );
  }

  if (response.status === 204) return undefined as T;

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return (await response.text()) as T;
  }

  return (await response.json()) as T;
}
