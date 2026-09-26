/**
 * 前端开发期配置的唯一出口。
 *
 * 规则（与项目约定一致）：
 * - **敏感信息只走环境变量**：这里只读 `VITE_*` 这类会打进前端包的非敏感配置，
 *   密码 / Token / 密钥一律不写进代码，也不写进 `.env`；
 * - 集中在一处读取，避免 `import.meta.env` 散落在组件里；
 * - 只做「取值 + 兜底」，不做校验、不抛异常 —— 配置缺失时由调用方走失败分支，
 *   绝不让页面白屏。
 */

/**
 * 后端服务地址（**浏览器侧**的概念）。
 *
 * 开发期刻意留空 → 请求走**相对路径** `/api/exams`，由 Vite 开发服务器
 * 反代到 Spring Boot（见 `vite.config.ts` 的 `server.proxy`）。
 * 这样做是必须的：后端只监听回环地址且**没有任何 CORS 配置**，
 * 浏览器直连会被拦下来（实测 `net::ERR_FAILED`，响应体读不到），
 * 而同源相对路径不产生跨源请求，天然可读。
 *
 * 需要直连后端（例如起静态服务预览 `dist`、或后端已配好 CORS）时，
 * 在 `.env` 里设置 `VITE_API_BASE_URL=http://localhost:8080` 即可覆盖。
 * 末尾斜杠统一去掉，避免拼出 `//api/exams`。
 */
export const API_BASE_URL: string = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')

/**
 * 前端开发阶段的**临时固定测试学生**。
 *
 * 说明（刻意保留这个注释，方便后续替换）：
 * - F3 阶段没有登录体系、不做 JWT，`studentId` 只能先用种子数据里的固定学号；
 * - 后端 `data.sql` 已为 `student-001` 准备了两场考试，
 *   `student-002` 用于验证「换个学号数据确实不同」；
 * - 接入真实身份认证后，这里应改为从登录态读取，而不是环境变量。
 */
export const CAMPUS_STUDENT_ID: string = import.meta.env.VITE_CAMPUS_STUDENT_ID ?? 'student-001'

/** 兜底学生学号：环境变量被清空时使用，保证开发期不会因为漏配而必失败 */
export const DEFAULT_STUDENT_ID = 'student-001'

/** 取本次请求真正使用的学号（环境变量为空串时回落到默认值） */
export function resolveStudentId(): string {
  return CAMPUS_STUDENT_ID.trim() || DEFAULT_STUDENT_ID
}

/** 把相对路径拼成完整 URL（后端地址允许带子路径，因此只做简单拼接） */
export function buildApiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`
}

/** 请求超时（毫秒）。与 Axios 侧保持同一量级，避免两套客户端行为差太多 */
export const REQUEST_TIMEOUT_MS = 15_000
