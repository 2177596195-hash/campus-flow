# 前端第三阶段课程表 API 接入完成报告（F3-2）

> 阶段目标：在 F3-1（考试安排真实 API）的基础上，把「课程表」查询接入真实后端，
> 形成 **Vue → Vite proxy → Spring Boot → MySQL** 的完整链路。
> 本阶段只做课程表 API 接入；未接入成绩、请假、Agent、MCP、RAG。

## 一、改动清单

### 新增（campus-web，8 个文件）

| 文件 | 职责 |
| --- | --- |
| `src/types/course.ts` | 课程领域类型。逐字对照后端 `CourseScheduleResponse.java` record：六字段全 string（`course/teacher/weekday/time/room/weeks`），无外层包裹；`CourseQuery` 含可选 `week?: number` |
| `src/api/courses.ts` | 课程 API 模块（顶层模块，fetch 不用 Axios，与 `exams.ts` 同款）：`COURSES_PATH='/api/courses'`、`CourseApiError{network\|http\|malformed}`、超时与外部 AbortSignal 合成一路取消、严格六字段校验、`formatCourseSchedules()`；**week 只在明确给出数字时才进 URL**，`studentId`/`week` 均不写死 |
| `src/constants/courseIntent.ts` | 课程意图关键词判定（`isCourseQuery`）+ 周次抽取（`parseCourseWeek`：只认「第 N 周」，抽不到返回 undefined，**绝不猜「这周=第几周」**）；与考试关键词刻意互斥 |
| `src/lib/courseFlow.ts` | 课程查询编排层：`runCourseQuery(week, signal?)` —— **week 是显式第一参数，无默认值**；永不抛异常、失败归一成「课表查询失败」、绝不编造数据；`shouldRenderReply` / `toAssistantMessage` 与 examFlow 同款 |
| `scripts/verify-f3-course-contract.mjs` | 静态源码契约核对（19 项） |
| `scripts/verify-f3-course-flow.mjs` | 模块级链路验证（SSR 加载真实源码 + 真实 HTTP，34 项） |
| `scripts/verify-f3-course-failure-path.mjs` | 失败路径验证（不可达地址，22 项） |
| `scripts/verify-f3-course-edge.mjs` | 真实浏览器端到端验证（Edge + CDP，34 项） |

### 修改（3 个文件，均为小幅）

| 文件 | 改动 |
| --- | --- |
| `src/views/ChatView.vue` | 分流从两条变三条：考试（F3-1 原样）→ 课程（新）→ mock 兜底；新增 `replyWithCourses(week)`（与 `replyWithExams` 同一套 pending 收尾机制）；调试入口追加 `__campusFlowCourseQuery` |
| `src/lib/mockReply.ts` | 课表兜底规则文案改为「已接通真实后端接口」（与考试规则同款），不再声称未接入 |
| `.env.example` | 相对路径说明从 `/api/exams` 推广为 `/api/*` |

## 二、后端课程 API 真实契约（以 Spring Boot 实际返回为准）

`GET /api/courses?studentId=xxx&week=3`（`CourseController` → `CourseService` → `CourseScheduleRepository`）

- **响应**：`List<CourseScheduleResponse>`，JSON 数组、**无外层包裹**，每项六字段**全字符串**：

```json
{
  "course": "数据结构",
  "teacher": "李明",
  "weekday": "周一",
  "time": "08:00-09:40",
  "room": "A302",
  "weeks": "1-16周"
}
```

- `weekday` 与 `weeks` 是 DTO 渲染好的展示字符串（库里存 1..7 序号与 `week_start/week_end` 区间），前端不做换算。
- 不泄露实体字段（`id/studentId/createdAt/classTime/weekStart/weekEnd`）。
- **参数行为（实测）**：
  - `studentId` 必填，缺失 → **400**（Spring 默认错误体）；
  - `week` 可选：不传 = 整学期；非数字（`week=abc`）→ **400**；
  - 查无数据（不存在的学生 / 没有课程覆盖的周）→ **200 + `[]`**，不是 404；
  - week 过滤在数据库完成（`week_start <= week <= week_end`）：种子数据里「数据库原理」5-16 周开课，**week=3 查不到（7 门）、week=8 查得到（8 门）**。

## 三、前端调用方式

- 默认走**相对路径** `/api/courses`（`VITE_API_BASE_URL` 留空），由 Vite dev server `server.proxy` 反代到 `CAMPUS_API_TARGET`（本次验证指向 `http://localhost:8081`）——与 F3-1 完全同一套同源方案，绕开后端无 CORS 的问题。
- `fetchCourseSchedules(query: CourseQuery, signal?: AbortSignal)`：`studentId` 必传；`week` 为 `number | undefined`，undefined = 整学期，**只有数字才拼进查询串**。
- 开发期学号集中管理于 `src/config/env.ts`（`VITE_CAMPUS_STUDENT_ID=student-001`，`resolveStudentId()` 兜底），业务模块零硬编码。
- 聊天入口分流：`isExamQuery` → `isCourseQuery`（命中后 `parseCourseWeek` 抽周次；「查看第3周课程」→ 3，「这周/我的课程表」→ 整学期）→ mock 兜底。

## 四、验证结果汇总

### A. 后端真实接口（BASE_URL=http://localhost:8081）

| 场景 | 结果 |
| --- | --- |
| `studentId=student-001&week=3` | 200，7 条真实课程 |
| `studentId=student-001&week=8` | 200，8 条（含数据库原理）——week 过滤真实生效 |
| `studentId=student-001`（无 week） | 200，8 条（整学期） |
| `studentId=student-999&week=3` | 200，`[]` |
| 缺 `studentId` | 400 |
| `week=abc` | 400 |

### B. 模块级（SSR 加载真实源码）

- `verify-f3-course-flow.mjs`：**34/34 通过**（意图路由、周次抽取边界、API 直调、编排层、绝不编造数据）
- `verify-f3-course-failure-path.mjs`：**22/22 通过**（不可达地址 → 不抛异常、统一失败前缀、零编造数据、CourseApiError(network)）
- `verify-f3-course-contract.mjs`：**19/19 通过**（源码契约）
- F3-1 回归：`verify-f3-contract.mjs` **26/26**、`verify-f3-exam-flow.mjs` **9/9**、`verify-f3-failure-path.mjs` **21/21** —— 考试链路分毫不损

### C. 浏览器真实链路（Edge 153 + CDP，`http://localhost:5173`）

**34/34 通过**，完整链路 `Vue → Vite proxy（localhost:5173/api/courses）→ Spring Boot(:8081) → MySQL → 返回课程数据 → 页面展示`：

1. 输入「查看第3周课程」→ 页面发出 `GET http://localhost:5173/api/courses?studentId=student-001&week=3` → 200（`acao=null`，同源标志）→ 聊天区渲染 7 门真实课程，且「数据库原理」**不出现**；
2. 输入「我的课程表」→ 请求**不带 week** → 渲染 8 门整学期课程，「数据库原理」这次**出现**——反证第一次 week 过滤真实生效；
3. 输入「查看第20周课程」→ 200 + `[]` → 渲染正常回复「这一周没有课程。」，无「失败」标记、无编造数据。

全程零 CORS 失败、零 console 错误、无「占位」标签。

### D. 构建 / 类型检查 / 测试

- `npm run type-check`（vue-tsc --build）：**通过，0 错误**
- `npm run build`（vue-tsc --build && vite build）：**通过**（62 modules，dist 产出正常）
- 课程 API/流程测试：上述四个 verify 脚本全部通过（沿用项目「SSR 渲染真实模块」的验证姿势，未引入 vitest）

## 五、边界遵守情况

- **未修改** `campus-service` / `campus-agent` / Java 后端 / 数据库结构；后端课程接口本身未发现任何问题（所有实测行为与源码注释一致），无需记录缺陷。
- 未接入 Agent / LangGraph / MCP / RAG / 登录；未重构 F2 UI、未引入 UI 组件库；未删除 F2/F3-1 验证脚本（全部仍在并通过回归）。
- **未提交**：按要求保留工作区改动待验收。

## 六、Git 状态（验收时点）

- 修改（3）：`campus-web/.env.example`、`campus-web/src/lib/mockReply.ts`、`campus-web/src/views/ChatView.vue`
- 新增（8）：`campus-web/src/{types/course.ts, api/courses.ts, constants/courseIntent.ts, lib/courseFlow.ts}`、`campus-web/scripts/verify-f3-course-{contract,flow,failure-path,edge}.mjs`
- 基线：`25ae5ef feat(frontend): integrate real exam API`（本阶段未 commit）
