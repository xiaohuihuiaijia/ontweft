import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8000' } },
  test: { environment: 'jsdom', restoreMocks: true, setupFiles: ['./src/test/test-setup.ts'] },
});
