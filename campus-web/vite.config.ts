import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 后端服务地址：**仅 Vite 开发服务器使用**（写进浏览器的是相对路径，见下）。
 *
 * - 默认与 `campus-service` 的 `SERVER_PORT` 默认值一致（8080）；
 * - 若后端换端口启动，用 `CAMPUS_API_TARGET` 覆盖，**不要改这个默认值**：
 *      $env:CAMPUS_API_TARGET='http://127.0.0.1:8091'; npm run dev
 * - 刻意**不读 `VITE_API_BASE_URL`**：那个变量是给浏览器里的代码看的
 *   （决定发相对路径还是绝对 URL），把它和代理目标绑在一起会导致
 *   「一改就把两边同时改坏」，两个概念必须分开。
 */
const CAMPUS_API_TARGET = process.env.CAMPUS_API_TARGET ?? 'http://127.0.0.1:8080'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    /**
     * 把 `/api` 反代到 Spring Boot —— 这是绕开 CORS 的关键：
     * 后端只监听回环地址且没有任何 CORS 配置，浏览器直接请求它会被拦
     * （实测 `net::ERR_FAILED`）。改为浏览器请求同源的 `/api/...`，
     * 由开发服务器在 Node 侧转发，请求就不再有跨源属性。
     *
     * 注意代理目标用**主机名 `localhost`** 而不是写死的 `127.0.0.1`：
     * 本机后端在 IPv6 回环上的表现更稳定（Node 侧 `localhost` 会按需回落到
     * IPv4）。若要指定 IPv4，请显式写 `http://127.0.0.1:<port>`。
     * 生产环境应由网关 / 反向代理承担同样的同源暴露职责。
     */
    proxy: {
      '/api': {
        target: CAMPUS_API_TARGET,
        changeOrigin: true,
      },
    },
  },
})
