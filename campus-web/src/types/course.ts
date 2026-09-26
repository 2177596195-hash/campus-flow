/**
 * 课程安排（Course）领域的前端类型。
 *
 * 契约来源：**不猜字段**，逐字对照后端 Spring Boot 源码
 * `campus-service/src/main/java/edu/ylu/campus/dto/CourseScheduleResponse.java`
 * 中的 record 定义：
 *
 * ```java
 * public record CourseScheduleResponse(
 *         String course, String teacher, String weekday,
 *         String time, String room, String weeks) {}
 * ```
 *
 * 并已于 2026-09-26 用真实服务（SERVER_PORT=8081）逐项核对实际返回：
 * `GET /api/courses?studentId=student-001&week=3` → 200 + 七条记录，
 * 每条形如：
 * ```json
 * {
 *   "course": "数据结构", "teacher": "李明", "weekday": "周一",
 *   "time": "08:00-09:40", "room": "A302", "weeks": "1-16周"
 * }
 * ```
 *
 * 五个必须记住的契约细节（都有后端测试兜底，见 `CourseControllerTest`）：
 * 1. **没有外层包裹**：Controller 直接返回 `List<CourseScheduleResponse>`，
 *    不是 `{ code, message, data }`，所以不能用 `types/api.ts` 里的 `ApiResponse`。
 * 2. **六个字段全部是字符串**：库里 `weekday` 存 1..7 序号、周次存
 *    `week_start` / `week_end` 两个整数，但**对外都被 DTO 渲染成了展示字符串**
 *    （「周一」 /「1-16周」），前端拿到的就是字符串，不需要也不应该再换算。
 * 3. **不泄露实体字段**：`id` / `studentId` / `createdAt` / `classTime` /
 *    `weekStart` / `weekEnd` 一律不存在；上课时段字段名是 `time`（不是实体的
 *    `classTime`），周次区间字段名是 `weeks`（复数，不是 `week`）。
 * 4. **查无数据是 200 + `[]`**，不是 404 —— 空数组是正常结果（该生这周没课），
 *    不能当成错误；不存在的学生与「没选课」在这层无法区分，都是空数组。
 * 5. **week 过滤在数据库完成**：种子数据里「数据库原理」5-16 周才开课，
 *    `week=3` 查不到它、`week=8` 查得到——因此「week 参数真的进了请求」
 *    可以被直接断言，而不是只看条数变化。
 */

/**
 * 一条课程安排。
 *
 * 字段全部来自后端 DTO，不做任何重命名或类型转换，
 * 让「后端契约 → 前端类型」保持可逐行对照，避免维护时对不上。
 */
export interface CourseSchedule {
  /** 课程名称 */
  course: string
  /** 任课教师 */
  teacher: string
  /** 星期，后端已渲染为「周一」…「周日」 */
  weekday: string
  /** 上课时段，后端固定为 `HH:mm-HH:mm`，形如 `08:00-09:40` */
  time: string
  /** 教室 */
  room: string
  /** 上课周次区间，后端已渲染为「1-16周」 */
  weeks: string
}

/** 课程查询接口的入参 */
export interface CourseQuery {
  /** 学号。后端 `@RequestParam("studentId")` 为必填，缺失返回 400 */
  studentId: string
  /**
   * 教学周序号（可选）。后端 `required = false`：
   * 传入时只返回「覆盖该周」的课程；不传（undefined）返回整学期课表。
   *
   * 类型必须是 `number | undefined`：`null` / `0` / 空串都**不是**「不传」，
   * 拼查询串时也绝不把这三个值当成「不传」处理。
   */
  week?: number
}

/** 请求最终失败的原因分类（仅用于前端提示措辞，不映射后端错误码） */
export type CourseApiErrorKind =
  /** 连不上后端：服务没启动、地址/端口不对、被 CORS 拦截 */
  | 'network'
  /** 后端返回了非 2xx（如缺少 studentId 的 400、week 非数字的 400、服务端 500） */
  | 'http'
  /** 响应不是合法 JSON，或结构与后端 DTO 不一致 */
  | 'malformed'
