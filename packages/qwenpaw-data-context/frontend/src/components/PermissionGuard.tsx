import React from 'react';
import type { Resource } from '@/auth';
import { useAuthStore } from '@/store/authStore';

interface PermissionGuardProps {
  resource: Resource;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

const PermissionGuard: React.FC<PermissionGuardProps> = ({ resource, children, fallback }) => {
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const capabilities = useAuthStore((s) => s.capabilities);

  if (!authEnabled) return <>{children}</>;

  if (capabilities.includes(resource)) {
    return <>{children}</>;
  }

  if (fallback) return <>{fallback}</>;

  const NoPermissionPage = React.lazy(() => import('@/pages/NoPermissionPage'));
  return (
    <React.Suspense fallback={null}>
      <NoPermissionPage resource={resource} />
    </React.Suspense>
  );
};

export default PermissionGuard;
