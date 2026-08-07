import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

function isLoopbackHost(host: string | boolean | undefined): boolean {
  if (host === undefined || host === false) return true;
  if (host === true) return false;
  const normalized = host.trim().toLowerCase().replace(/^\[|\]$/g, '');
  return normalized === 'localhost'
    || normalized === '::1'
    || normalized === '127.0.0.1';
}

function authenticationConfigured(env: Record<string, string>): boolean {
  if ((env.DATAPAW_API_TOKEN || '').trim()) return true;
  const rawKeys = (env.DATAPAW_API_KEYS || '').trim();
  if (!rawKeys) return false;
  try {
    const keys: unknown = JSON.parse(rawKeys);
    return Array.isArray(keys) && keys.length > 0;
  } catch {
    return false;
  }
}

export default defineConfig(({ mode }) => {
  const repoRoot = path.resolve(__dirname, '../../..');
  const env = loadEnv(mode, repoRoot, '');
  const serviceBaseUrl = env.VITE_API_BASE_URL || env.SERVICE_BASE_URL || 'http://localhost:8765';
  const allowedHosts = [
    'localhost',
    '127.0.0.1',
    ...((env.DATAPAW_FRONTEND_ALLOWED_HOSTS || '')
      .split(',')
      .map((host) => host.trim())
      .filter(Boolean)),
  ];

  return {
    envDir: repoRoot,
    plugins: [
      react(),
      {
        name: 'datapaw-public-frontend-auth-guard',
        configResolved(config) {
          if (
            config.command === 'serve'
            && !isLoopbackHost(config.server.host)
            && !authenticationConfigured(env)
          ) {
            throw new Error(
              'Refusing to expose the DataPaw frontend without API authentication. '
              + 'Configure DATAPAW_API_TOKEN or DATAPAW_API_KEYS, or bind to 127.0.0.1.',
            );
          }
        },
      },
    ],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
        crypto: 'crypto-browserify',
      },
    },
    css: {preprocessorOptions: {less: {javascriptEnabled: true},},},
    server: {
      // 默认仅监听本机回环；需对外暴露时显式设置 FRONTEND_HOST=0.0.0.0
      host: env.FRONTEND_HOST || '127.0.0.1',
      port: 3000,
      strictPort: true,
      allowedHosts,
      proxy: {
        '/api': {
          target: serviceBaseUrl,
          changeOrigin: true,
        },
        '/web': {
          target: serviceBaseUrl,
          changeOrigin: true,
        },
      },
    },
    define: {
      // 一定要序列化，否则打包时会报错
      SERVICE_BASE_URL: JSON.stringify(serviceBaseUrl),
      'process.env.SERVICE_BASE_URL': JSON.stringify(serviceBaseUrl),
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      minify: 'terser' as const,
      rollupOptions: {
        output: {
          entryFileNames: 'assets/index.js',
          chunkFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
          manualChunks(moduleId) {
            if (!moduleId.includes('node_modules')) return undefined;
            if (moduleId.includes('/@antv/')) return 'vendor-graph';
            if (
              moduleId.includes('/@codemirror/')
              || moduleId.includes('/@lezer/')
              || moduleId.includes('/@uiw/')
              || moduleId.includes('/@ant-design/pro-components/')
              || moduleId.includes('/@agentscope-ai/design/')
              || moduleId.includes('/antd/')
              || moduleId.includes('/@ant-design/icons/')
              || moduleId.includes('/@rc-component/')
              || moduleId.includes('/react/')
              || moduleId.includes('/react-dom/')
              || moduleId.includes('/react-router/')
              || moduleId.includes('/react-i18next/')
              || moduleId.includes('/i18next/')
              || moduleId.includes('/zustand/')
            ) return 'vendor-ui';
            if (moduleId.includes('/@e965/xlsx/')) return 'vendor-xlsx';
            return undefined;
          },
        },
      },
      cssCodeSplit: true,
    },
  }
});
