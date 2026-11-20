import { useEffect, useLayoutEffect, useSyncExternalStore } from 'react';
import { message } from 'antd';
import { useLocation, useNavigate } from 'react-router-dom';
import { configureUiBridge } from '@qwenpaw-data/cm';
import { getHostCurrentUser } from '../../../services/auth';
import { uiRegistry } from './registry';
import { setUiSession, getUiActivity, subscribeUiActivity } from './runtime';

configureUiBridge(uiRegistry);
export function RuntimeProvider({ sessionId }: { sessionId: string | null }) {
  const activity = useSyncExternalStore(subscribeUiActivity, getUiActivity);
  const [messageApi, contextHolder] = message.useMessage();
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    const key = 'agent-ui-activity';
    if (activity) {
      const pending = activity.startsWith('正在') || activity.includes('正在回传');
      void messageApi.open({
        key,
        type: pending ? 'loading' : 'info',
        content: activity,
        duration: pending ? 0 : 3,
      });
    } else {
      messageApi.destroy(key);
    }
    return () => messageApi.destroy(key);
  }, [activity, messageApi]);
  useLayoutEffect(() => {
    uiRegistry.setPath(location.pathname);
    uiRegistry.navigate = (path) =>
      navigate(
        path === '/console' && sessionId
          ? `/console?session_id=${encodeURIComponent(sessionId)}`
          : path,
      );
    uiRegistry.authorize = (page) => {
      const user = getHostCurrentUser();
      const capability = page.startsWith('graph_')
        ? 'mg:read'
        : page === 'kg_docs'
          ? 'tg_kg:self'
          : page === 'weaving'
            ? 'semantic_weaving'
            : page === 'model_settings'
              ? 'model_config'
              : 'datasource:read';
      if (
        !user ||
        user.role === 'platform_admin' ||
        (page !== 'artifact' && !user.capabilities?.includes(capability))
      )
        throw new Error('FORBIDDEN');
    };
  }, [location.pathname, navigate, sessionId]);
  useEffect(() => {
    setUiSession(sessionId);
  }, [sessionId]);
  useEffect(() => () => setUiSession(null), []);
  useEffect(() => {
    const changed = (event: Event) => {
      if (event.isTrusted) uiRegistry.userChanged();
    };
    document.addEventListener('pointerdown', changed, true);
    document.addEventListener('keydown', changed, true);
    return () => {
      document.removeEventListener('pointerdown', changed, true);
      document.removeEventListener('keydown', changed, true);
    };
  }, []);
  return contextHolder;
}
