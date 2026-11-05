import { capabilities } from './services/capabilities';
import { ConfigProvider } from '@agentscope-ai/design';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import ConsoleLayout from './layouts/ConsoleLayout';
import AgentSettingsPage from './pages/AgentSettings';
import ChannelSettingsPage from './pages/ChannelSettings';
import ConsolePage from './pages/Console';
import CronJobsPage from './pages/CronJobs';
import SessionsPage from './pages/Sessions';
import McpHubPage from './pages/McpHub';
import SkillsHubPage from './pages/SkillsHub';
import { hostThemeProviderProps } from './theme';

export default function App() {
  return (
    <ConfigProvider {...hostThemeProviderProps}>
      <BrowserRouter>
        <Routes>
          <Route path="/console" element={<ConsoleLayout />}>
            <Route index element={<ConsolePage />} />
            <Route path="sessions" element={<SessionsPage />} />
            {capabilities.skillsHub && <Route path="skills" element={<SkillsHubPage />} />}
            {capabilities.mcpHub && <Route path="mcp" element={<McpHubPage />} />}
            <Route path="cron-jobs" element={<CronJobsPage />} />
            <Route path="settings/agent" element={<AgentSettingsPage />} />
            {capabilities.channels && <Route path="settings/channels" element={<ChannelSettingsPage />} />}
          </Route>
          <Route path="*" element={<Navigate replace to="/console" />} />
        </Routes>
      </BrowserRouter>
    </ConfigProvider>
  );
}
