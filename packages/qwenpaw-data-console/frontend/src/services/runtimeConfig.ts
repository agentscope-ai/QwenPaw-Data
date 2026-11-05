export interface ConsoleRuntimeConfig {
  title: string;
  assistant_name: string;
  welcome_title: string;
  welcome_description: string;
  workspace_name: string;
  logo_url: string;
  avatar_url: string;
  sample_questions: string[];
  capabilities: {
    skillsHub: boolean;
    mcpHub: boolean;
    channels: boolean;
    contextConsole: boolean;
  };
}

declare global {
  interface Window { QWENPAW_DATA_CONSOLE?: ConsoleRuntimeConfig; }
}

const defaults: ConsoleRuntimeConfig = {
  title: 'QwenPaw Data',
  assistant_name: 'Data Analyst',
  welcome_title: '开始新的数据分析',
  welcome_description: '选择数据源并描述你希望验证的业务问题。',
  workspace_name: '本地数据空间',
  logo_url: '/qwenpaw-data-brand.png',
  avatar_url: '/mascot-blue-transparent.png',
  sample_questions: [],
  capabilities: { skillsHub: false, mcpHub: false, channels: false, contextConsole: false },
};

export function getRuntimeConfig(): ConsoleRuntimeConfig {
  const value = typeof window === 'undefined' ? undefined : window.QWENPAW_DATA_CONSOLE;
  return { ...defaults, ...value, capabilities: { ...defaults.capabilities, ...value?.capabilities } };
}
