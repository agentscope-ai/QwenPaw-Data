import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { frontendEnvironment, devLoginConfig } from './devLoginConfig';

function isLoopbackHost(host: string | boolean | undefined): boolean {
  if (host === undefined || host === false) return true;
  if (host === true) return false;
  const normalized = host.trim().toLowerCase().replace(/^\[|\]$/g, '');
  return normalized === 'localhost'
    || normalized === '::1'
    || normalized === '127.0.0.1';
}

function authenticationConfigured(env: Record<string, string | undefined>): boolean {
  if ((env.QWENPAW_DATA_API_TOKEN || '').trim()) return true;
  const rawKeys = (env.QWENPAW_DATA_API_KEYS || '').trim();
  if (!rawKeys) return false;
  try {
    const keys: unknown = JSON.parse(rawKeys);
    return Array.isArray(keys) && keys.length > 0;
  } catch {
    return false;
  }
}

export default defineConfig(({ mode, command }) => {
  const consoleBuild = mode === 'console';
  const repoRoot = path.resolve(__dirname, '../../..');
  // Console release builds are independent of the publisher's private .env.
  const environment = consoleBuild ? { env: {}, envFile: '' } : frontendEnvironment(mode, repoRoot, process.env);
  const env = environment.env;
  const devLogin = devLoginConfig(command, repoRoot, env, environment.envFile);
  const serviceBaseUrl = env.VITE_API_BASE_URL || env.SERVICE_BASE_URL || 'http://localhost:8765';
  const authBaseUrl = env.VITE_AUTH_API_URL || env.VITE_AUTH_URL || 'http://localhost:8088';
  // 登录页联调账号快填条的口令：仅 dev server 保留 .env 里的值，
  // 构建时强制置空，避免口令被 inline 进产物。
  const allowedHosts = [
    'localhost',
    '127.0.0.1',
    ...((env.QWENPAW_DATA_FRONTEND_ALLOWED_HOSTS || '')
      .split(',')
      .map((host) => host.trim())
      .filter(Boolean)),
  ];

  return {
    base: consoleBuild ? '/databridge/' : '/',
    envDir: consoleBuild ? false : repoRoot,
    plugins: [
      react(),
      ...(consoleBuild ? [{
        name: 'qwenpaw-data-console-config',
        transformIndexHtml() {
          return [{ tag: 'script', attrs: { src: '/console-config.js' }, injectTo: 'head-prepend' as const }];
        },
      }] : []),
      {
        name: 'qwenpaw-data-public-frontend-auth-guard',
        configResolved(config: { command: string; server: { host?: string | boolean } }) {
          if (
            config.command === 'serve'
            && !isLoopbackHost(config.server.host)
            && !authenticationConfigured(env)
          ) {
            throw new Error(
              'Refusing to expose the QwenPaw Data frontend without API authentication. '
              + 'Configure QWENPAW_DATA_API_TOKEN or QWENPAW_DATA_API_KEYS, or bind to 127.0.0.1.',
            );
          }
        },
      },
    ],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
        '@qwenpaw-data/ui-tokens': path.resolve(__dirname, '../../qwenpaw-data-ui-tokens/src'),
        'react/jsx-dev-runtime': path.resolve(__dirname, 'node_modules/react/jsx-dev-runtime.js'),
        'react/jsx-runtime': path.resolve(__dirname, 'node_modules/react/jsx-runtime.js'),
        react: path.resolve(__dirname, 'node_modules/react/index.js'),
        crypto: 'crypto-browserify',
      },
      dedupe: ['react', 'react-dom'],
    },
    css: { preprocessorOptions: { less: { javascriptEnabled: true } } },
    server: {
      // 默认仅监听本机回环；需对外暴露时显式设置 FRONTEND_HOST=0.0.0.0
      host: env.FRONTEND_HOST || '127.0.0.1',
      port: 3000,
      strictPort: true,
      allowedHosts,
      proxy: {
        '/api/auth': {
          target: authBaseUrl,
          changeOrigin: true,
        },
        '/api/platform': {
          target: authBaseUrl,
          changeOrigin: true,
        },
        '/api/users': {
          target: authBaseUrl,
          changeOrigin: true,
        },
        '/api/roles': {
          target: authBaseUrl,
          changeOrigin: true,
        },
        '/api/workspaces': {
          target: authBaseUrl,
          changeOrigin: true,
        },
        '/api/tenants': {
          target: authBaseUrl,
          changeOrigin: true,
        },
        '/api/access-requests': {
          target: authBaseUrl,
          changeOrigin: true,
        },
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
      'import.meta.env.VITE_DEV_LOGIN_ACCOUNTS': JSON.stringify(JSON.stringify(devLogin.accounts)),
      'import.meta.env.VITE_DEV_LOGIN_PASSWORD': JSON.stringify(devLogin.password),
      'import.meta.env.VITE_DEV_LOGIN_PASSWORD_OVERRIDES':
        JSON.stringify(devLogin.overrides),
    },
    envPrefix: 'VITE_',
    build: {
      outDir: 'dist',
      sourcemap: false,
      minify: 'terser' as const,
      rollupOptions: {
        output: {
          inlineDynamicImports: true,
          entryFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
        },
      },
      cssCodeSplit: false,
    },
  };
});
