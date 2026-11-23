import type { ReactNode } from 'react';
import { Input } from 'antd';
import { useSearchParams } from 'react-router-dom';

/** The host owns profile names; this generic control has no domain registry. */
export default function AgentSettingsScope({ children }: { children: (agentId: string) => ReactNode }) {
  const [params, setParams] = useSearchParams();
  const agentId = params.get('agent_id')?.trim() || 'default';
  return <>
    <div style={{ padding: '16px 24px 0' }}>
      <Input.Search key={agentId} aria-label="Agent profile" addonBefore="Agent"
        defaultValue={agentId} enterButton="→" style={{ maxWidth: 360 }}
        onSearch={value => {
          const id = value.trim() || 'default';
          setParams(previous => { const next = new URLSearchParams(previous); next.set('agent_id', id); return next; });
        }} />
    </div>
    {children(agentId)}
  </>;
}
