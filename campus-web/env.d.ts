/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_APP_TITLE: string
  // 后续接入 CampusFlow Backend 时在此补充 VITE_API_BASE_URL 等变量
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
