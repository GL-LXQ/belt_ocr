import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: { port: 1420, strictPort: true, proxy: { '/api': 'http://127.0.0.1:8765' } },
  clearScreen: false,
  test: { environment: 'jsdom', include: ['src/**/*.test.ts'], restoreMocks: true },
})
