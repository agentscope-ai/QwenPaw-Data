import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig, type ProxyOptions } from 'vite';

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

export default defineConfig({
  // Release assets must not embed machine-local .env values.
  envDir: false,
  plugins: [react()],
  resolve: { alias: { '@': path.resolve(__dirname, 'src') } },
  css: { preprocessorOptions: { less: { javascriptEnabled: true } } },
  server: {
    host: '127.0.0.1', port: 18769, strictPort: true,
    proxy: {
      '/console-config.js': gateway,
      '/api': gateway,
      '/portal': gateway,
      '/databridge': gateway,
    },
  },
  build: { outDir: 'dist', sourcemap: false, minify: 'terser' },
});
