/**
 * 考试安排（Exam）领域的前端类型。
 *
 * 契约来源：**不猜字段**，逐字对照后端 Spring Boot 源码
 * `campus-service/src/main/java/edu/ylu/campus/dto/ExamScheduleResponse.java`
 * 中的 record 定义：
 *
 * ```java
 * public record ExamScheduleResponse(String course, String date, String time, String room) {}
 * ```
 *
 * 因此对外 JSON 是**四个字段、全部为字符串**的数组，例如：
 * ```json
 * [{ "course": "计算机网络", "date": "2026-09-28", "time": "14:00", "room": "A302" }]
 * ```
 *
 * 三个必须记住的契约细节（都有后端测试兜底，见 `ExamControllerTest`）：
 * 1. **没有外层包裹**：Controller 直接返回 `List<ExamScheduleResponse>`，
 *    不是 `{ code, message, data }`，所以不能用 `types/api.ts` 里的 `ApiResponse`。
 * 2. **不泄露实体字段**：`id` / `studentId` / `createdAt` / `examDate` 一律不存在，
 *    日期字段名是 `date`（不是 `examDate`），时间字段名是 `time`（不是 `examTime`）。
 * 3. **查无数据是 200 + `[]`**，不是 404 —— 空数组是正常结果，不能当成错误。
 */

/**
 * 一条考试安排。
 *
 * 字段全部来自后端 DTO，不做任何重命名或类型转换，
 * 让「后端契约 → 前端类型」保持可逐行对照，避免维护时对不上。
 */
export interface ExamSchedule {
  /** 考试科目 */
  course: string
  /** 考试日期，后端固定为 `yyyy-MM-dd` */
  date: string
  /** 开考时间，后端固定为 `HH:mm` */
  time: string
  /** 考场 */
  room: string
}

/** 考试查询接口的入参 */
export interface ExamQuery {
  /** 学号。后端 `@RequestParam("studentId")` 为必填，缺失返回 400 */
  studentId: string
}

/** 请求最终失败的原因分类（仅用于前端提示措辞，不映射后端错误码） */
export type ExamApiErrorKind =
  /** 连不上后端：服务没启动、地址/端口不对、被 CORS 拦截 */
  | 'network'
  /** 后端返回了非 2xx（如缺少 studentId 的 400、服务端 500） */
  | 'http'
  /** 响应不是合法 JSON，或结构与后端 DTO 不一致 */
  | 'malformed'
