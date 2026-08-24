export type LLMProvider =
  | "dashscope"
  | "openai"
  | "deepseek"
  | "zhipu"
  | "siliconflow"
  | "volcengine"
  | "ollama"
  | "custom";

export interface ProviderPreset<T extends string> {
  value: T;
  label: string;
  baseUrl: string;
}

export const LLM_PROVIDER_PRESETS: readonly ProviderPreset<LLMProvider>[] = [
  {
    value: "dashscope",
    label: "DashScope",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  },
  { value: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1" },
  { value: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com" },
  {
    value: "zhipu",
    label: "智谱 GLM",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
  },
  {
    value: "siliconflow",
    label: "SiliconFlow",
    baseUrl: "https://api.siliconflow.cn/v1",
  },
  {
    value: "volcengine",
    label: "火山引擎",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
  },
  { value: "ollama", label: "Ollama", baseUrl: "http://localhost:11434/v1" },
  { value: "custom", label: "Custom", baseUrl: "" },
];

export function inferProvider<T extends string>(
  baseUrl: string | null | undefined,
  presets: readonly ProviderPreset<T>[],
  fallback: T,
): T {
  const normalized = (baseUrl ?? "").replace(/\/$/, "");
  const match = presets.find(
    (preset) => preset.baseUrl && preset.baseUrl.replace(/\/$/, "") === normalized,
  );
  return match?.value ?? fallback;
}

export function getProviderBaseUrl<T extends string>(
  value: T,
  presets: readonly ProviderPreset<T>[],
): string {
  return presets.find((preset) => preset.value === value)?.baseUrl ?? "";
}
