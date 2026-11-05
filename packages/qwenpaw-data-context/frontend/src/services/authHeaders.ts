import { getConsoleConfig } from "./consoleConfig";
import { getApiToken } from "./config";

export function buildAuthHeaders(): Record<string, string> {
  if (getConsoleConfig()?.auth_mode === "portal") return { "X-QwenPaw-Console": "1" };
  const token = getApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
