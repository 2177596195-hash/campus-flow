/**
 * 成绩（Score）领域的前端类型。
 *
 * 契约来源：**不猜字段**，逐字对照后端 Spring Boot 源码
 * `campus-service/src/main/java/edu/ylu/campus/dto/ScoreResponse.java`
 * 中的 record 定义：
 *
 * ```java
 * public record ScoreResponse(
 *         String semester, String course,
 *         BigDecimal score, BigDecimal credit) {}
 * ```
 *
 * 并已于 2026-09-26 用真实服务（SERVER_PORT=8081）逐项核对实际返回：
 * `GET /api/scores?studentId=student-001&semester=2025-2026-1` → 200 + 三条记录，
 * 每条形如（**原样照抄自真实响应**）：
 * ```json
 * {
 *   "semester": "2025-2026-1", "course": "数据结构",
 *   "score": 92.00, "credit": 4.0
 * }
 * ```
 * 注意 `score` 带两位小数（DECIMAL(5,2)）、`credit` 带一位（DECIMAL(3,1)），
 * 都是合法 JSON 数字；`JSON.parse` 之后小数尾零不保留（92.00 → 92），
 * 因此渲染出来是「成绩：92」——这是数值语义下的正确显示，不是丢数据。
 *
 * 六个必须记住的契约细节（后端测试兜底见 `ScoreControllerTest`）：
 * 1. **没有外层包裹**：Controller 直接返回 `List<ScoreResponse>`，
 *    不是 `{ code, message, data }`，所以不能用 `types/api.ts` 里的 `ApiResponse`。
 * 2. **score 与 credit 是数值（JSON number），不是字符串** —— 这是与考试
 *    （四字段全 string）、课程（六字段全 string）最大的契约差异。后端用
 *    `BigDecimal` + `DECIMAL(5,2)` / `DECIMAL(3,1)` 保证十进制精确，
 *    Jackson 直接序列化为 JSON 数字；前端拿到的是 `number`，可直接比较与加权。
 * 3. **不泄露实体字段**：`id` / `studentId` / `createdAt` 一律不存在。
 * 4. **查无数据是 200 + `[]`**，不是 404 —— 空数组是正常结果
 *    （该生该学期没出分），不能当成错误；「学号不存在」与「该学期没成绩」
 *    在这层无法区分，都是空数组。
 * 5. **semester 是精确匹配**：`2025-2026-1` 与 `2024-2025-2` 是两个学期，
 *    后端**没有格式校验**——传不存在的学期（如 `2026-2027-1`）不会报错，
 *    只会精确匹配不到、返回 200 + `[]`。因此前端不需要「学期格式非法」
 *    的错误分支，解析层只产出格式正确的值即可。
 * 6. **排序由后端完成**：学期从新到旧（`ORDER BY semester DESC`，字典序与
 *    时间序一致）、同学期按课程名升序；前端不二次排序。
 */

/**
 * 一条成绩记录。
 *
 * 字段全部来自后端 DTO，不做任何重命名或类型转换，
 * 让「后端契约 → 前端类型」保持可逐行对照，避免维护时对不上。
 */
export interface ScoreRecord {
  /** 学期，形如 `2025-2026-1`（学年-学期序号，后端格式） */
  semester: string
  /** 课程名称 */
  course: string
  /** 成绩分数（0-100，数值） */
  score: number
  /** 学分（数值，可能带小数，如 3.5） */
  credit: number
}

/** 成绩查询接口的入参 */
export interface ScoreQuery {
  /** 学号。后端 `@RequestParam("studentId")` 为必填，缺失返回 400 */
  studentId: string
  /**
   * 学期（可选）。后端 `required = false`：
   * 传入时只返回该学期的成绩；不传（undefined）返回该学生**全部学期**的成绩
   * （按学期从新到旧排列）。
   *
   * 类型必须是 `string | undefined`：`null` / 空串都**不是**「不传」。
   * 「明确查全部学期」与「还没问用户要学期」在调用层是两件事，
   * 都会以 undefined 到达这里——区分它们的职责在意图层，不在 API 层。
   */
  semester?: string
}

/** 请求最终失败的原因分类（仅用于前端提示措辞，不映射后端错误码） */
export type ScoreApiErrorKind =
  /** 连不上后端：服务没启动、地址/端口不对、被 CORS 拦截 */
  | 'network'
  /** 后端返回了非 2xx（如缺少 studentId 的 400、服务端 500） */
  | 'http'
  /** 响应不是合法 JSON，或结构与后端 DTO 不一致 */
  | 'malformed'
