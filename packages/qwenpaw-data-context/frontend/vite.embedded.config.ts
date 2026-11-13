import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { esmExternalRequirePlugin } from 'rolldown/plugins';

const shared = [
  'react',
  'react-dom',
  'react-router',
  'react-router-dom',
  'antd',
  '@agentscope-ai/design',
  '@ant-design/icons',
  '@qwenpaw-data/ui-tokens',
];
export default defineConfig({
  envDir: false,
  plugins: [
    react(),
    // Context is consumed by Host's browser bundle, which has no Node require.
    esmExternalRequirePlugin({ external: shared.map((name) => new RegExp(`^${name}(?:/|$)`)) }),
  ],
  publicDir: false,
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      '@qwenpaw-data/ui-tokens': path.resolve(__dirname, '../../qwenpaw-data-ui-tokens/src'),
    },
  },
  define: {
    SERVICE_BASE_URL: JSON.stringify(''),
    'process.env.SERVICE_BASE_URL': JSON.stringify(''),
    'import.meta.env.VITE_DEV_LOGIN_ACCOUNTS': JSON.stringify('[]'),
    'import.meta.env.VITE_DEV_LOGIN_PASSWORD': JSON.stringify(''),
    'import.meta.env.VITE_DEV_LOGIN_PASSWORD_OVERRIDES': JSON.stringify(''),
  },
  css: {
    preprocessorOptions: { less: { javascriptEnabled: true } },
    postcss: {
      plugins: [
        {
          postcssPlugin: 'scope-context-reset',
          Rule(rule) {
            if (
              !rule.source?.input.file?.endsWith('/src/index.css') ||
              !rule.selector ||
              (rule.parent?.type === 'atrule' && /keyframes$/.test(rule.parent.name))
            )
              return;
            rule.selectors = rule.selectors.map((selector) =>
              selector === ':root' ||
              selector === 'html' ||
              selector === 'body' ||
              selector === '#root'
                ? '.cm-embedded'
                : `.cm-embedded ${selector}`,
            );
          },
        },
      ],
    },
  },
  build: {
    outDir: 'dist-embedded',
    minify: false,
    cssCodeSplit: false,
    lib: {
      entry: path.resolve(__dirname, 'src/embedded/index.tsx'),
      formats: ['es'],
      fileName: () => 'index.js',
      cssFileName: 'context',
    },
    rollupOptions: {
      output: { chunkFileNames: 'chunks/[name]-[hash].js' },
    },
  },
});
