import path from 'node:path';
import { cpSync } from 'node:fs';
import react from '@vitejs/plugin-react';
import { defineConfig, type ProxyOptions, type UserConfig } from 'vite';

const gateway: ProxyOptions = {
  target: 'http://127.0.0.1:18768', changeOrigin: true,
  configure(proxy) {
    proxy.on('proxyReq', (outgoing, incoming) => {
      if (incoming.headers.referer?.startsWith('http://127.0.0.1:18769/')) {
        outgoing.setHeader('Referer', incoming.headers.referer.replace('http://127.0.0.1:18769/', 'http://127.0.0.1:18768/'));
      }
      if (incoming.headers.origin === 'http://127.0.0.1:18769') {
        outgoing.setHeader('Origin', 'http://127.0.0.1:18768');
      }
    });
  },
};

export default defineConfig((): UserConfig => {
  const repoRoot = path.resolve(__dirname, '../../..');
  let outputDirectory = path.resolve(__dirname, 'dist');
  const directServices = process.env.QWENPAW_DATA_DIRECT_SERVICES === '1';
  const directProxy: Record<string, ProxyOptions> = {};
  if (directServices) {
    const authTarget = process.env.QWENPAW_DATA_AUTH_URL || 'http://127.0.0.1:8088';
    const contextTarget = process.env.SERVICE_BASE_URL || 'http://127.0.0.1:8765';
    for (const namespace of ['auth', 'platform', 'users', 'roles', 'workspaces', 'tenants', 'access-requests']) {
      directProxy[`/api/${namespace}`] = { target: authTarget, changeOrigin: true };
    }
    directProxy['/cm-api'] = { target: contextTarget, changeOrigin: true, rewrite: (value) => value.replace(/^\/cm-api/, '/api') };
    directProxy['/cm-web'] = { target: contextTarget, changeOrigin: true, rewrite: (value) => value.replace(/^\/cm-web/, '/web') };
    directProxy['/api'] = { target: process.env.QWENPAW_DATA_HOST_API_BASE_URL || 'http://127.0.0.1:8089', changeOrigin: true };
  }

  return {
    envDir: false,
    plugins: [react(), {
      name: 'context-public-assets',
      configResolved(config) { outputDirectory = path.resolve(config.root, config.build.outDir); },
      closeBundle() {
        cpSync(path.resolve(repoRoot, 'packages/qwenpaw-data-context/frontend/public'), outputDirectory,
          { recursive: true, force: false });
      },
    }],
    resolve: {
      alias: {
        '@qwenpaw-data/cm': path.resolve(__dirname, '../../qwenpaw-data-context/frontend/dist-embedded/index.js'),
        '@': path.resolve(__dirname, 'src'),
        '@qwenpaw-data/ui-tokens': path.resolve(__dirname, '../../qwenpaw-data-ui-tokens/src'),
        'react/jsx-dev-runtime': path.resolve(__dirname, 'node_modules/react/jsx-dev-runtime.js'),
        'react/jsx-runtime': path.resolve(__dirname, 'node_modules/react/jsx-runtime.js'),
        react: path.resolve(__dirname, 'node_modules/react/index.js'),
      },
      dedupe: ['react', 'react-dom', 'react-router', 'react-router-dom', 'antd', '@agentscope-ai/design', '@ant-design/icons'],
    },
    css: {
      preprocessorOptions: {
        less: {
          javascriptEnabled: true,
        },
      },
    },
    server: {
      host: '127.0.0.1',
      port: 18769,
      strictPort: true,
      proxy: directServices ? directProxy : {
        '/console-config.js': gateway,
        '/cm-api': gateway,
        '/cm-web': gateway,
        '/api': gateway,
        '/portal': gateway,
        '/databridge': gateway,
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      minify: 'terser',
      rollupOptions: {
        output: {
          entryFileNames: 'assets/[name]-[hash].js',
          assetFileNames: 'assets/[name]-[hash][extname]',
          // Keep Chat's circular re-exports together without absorbing shared styles.
          onlyExplicitManualChunks: true,
          manualChunks: (id) => id.includes('/node_modules/@agentscope-ai/chat/') ? 'agentscope-chat' : undefined,
        },
      },
      cssCodeSplit: false,
    },
  };
});
