import path from 'node:path';
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

  return {
    envDir: repoRoot,
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
        '@qwenpaw-data/ui-tokens': path.resolve(__dirname, '../../qwenpaw-data-ui-tokens/src'),
        'react/jsx-dev-runtime': path.resolve(__dirname, 'node_modules/react/jsx-dev-runtime.js'),
        'react/jsx-runtime': path.resolve(__dirname, 'node_modules/react/jsx-runtime.js'),
        react: path.resolve(__dirname, 'node_modules/react/index.js'),
      },
      dedupe: ['react', 'react-dom'],
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
      proxy: {
        '/console-config.js': gateway,
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
          // Let Rollup keep shared style initialization in dependency order.
          // Isolating chat alone creates a cycle with the entry's antd-style exports.
        },
      },
      cssCodeSplit: false,
    },
  };
});
