<script setup lang="ts">
/**
 * 聊天主页：侧边栏 + 消息区 + 输入区。
 *
 * F4 起数据流更新（成绩分支走 Agent）：
 *   1. **成绩**（Agent 链路，F4-1）：
 *      发送 → store 写入 user 消息 → 调用 Agent POST /api/agent/chat
 *      → Agent 内部完成意图识别、学期槽位填充、工具调用、自然语言生成
 *      → 前端展示 Agent 回复。多轮对话靠 threadId 维持（Agent checkpointer 管理状态）。
 *      **Agent 失败时回退** F3 的直连链路（`dispatchF3Score`），并按 network /
 *      http / malformed / timeout 区分失败原因——绝不把失败当成成功展示。
 *   2. **考试安排**（真实链路，F3-1，不变）：
 *      发送 → store 写入 user 消息 → 调用 Spring Boot `GET /api/exams`
 *   3. **课程表**（真实链路，F3-2，不变）：
 *      发送 → store 写入 user 消息 → 调用 Spring Boot `GET /api/courses`
 *   4. **请假申请**（真实链路，F3-4，不变）：
 *      发送 → store 写入 user 消息 → 调用 Spring Boot `POST /api/leaves`
 *   5. 其它问题（仍是本地占位）：
 *      发送 → store 写入 user 消息 → 本地占位回复。
 *
 * 分流顺序：**成绩在最前**——「考试成绩 / 期末成绩」里的「成绩 / 分数」
 * 指的是分数而非考场安排，必须进成绩分支。
 */

import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppSidebar from '@/components/sidebar/AppSidebar.vue'
import ChatInput from '@/components/chat/ChatInput.vue'
import ChatMessage from '@/components/chat/ChatMessage.vue'
import WelcomePanel from '@/components/chat/WelcomePanel.vue'
import {
  runAgentScoreChat,
  shouldRenderAgentReply,
  toAgentAssistantMessage,
} from '@/lib/agentScoreFlow'
import { isCourseQuery, parseCourseWeek } from '@/constants/courseIntent'
import { isExamQuery } from '@/constants/examIntent'
import { isLeaveQuery } from '@/constants/leaveIntent'
import { isScoreQuery, parseSemester, wantsAllSemesters } from '@/constants/scoreIntent'
import {
  runCourseQuery,
  shouldRenderReply as shouldRenderCourseReply,
  toAssistantMessage as toCourseAssistantMessage,
} from '@/lib/courseFlow'
import { shouldRenderReply, toAssistantMessage } from '@/lib/examFlow'
import { runExamQuery } from '@/lib/examFlow'
import {
  getLeaveFollowup,
  isLeaveComplete,
  parseLeaveInput,
  shouldRenderReply as shouldRenderLeaveReply,
  submitLeave,
  toAssistantMessage as toLeaveAssistantMessage,
} from '@/lib/leaveFlow'
import type { LeaveCollectedData } from '@/types/leave'
import { buildMockReply, MOCK_REPLY_DELAY_MS } from '@/lib/mockReply'
import {
  runScoreQuery,
  SCORE_SEMESTER_FOLLOWUP,
  shouldRenderReply as shouldRenderScoreReply,
  toAssistantMessage as toScoreAssistantMessage,
} from '@/lib/scoreFlow'
import { useConversationStore } from '@/stores/conversation'

const conversationStore = useConversationStore()

/** 侧边栏抽屉（仅窄屏有意义） */
const sidebarOpen = ref(false)

/** 在发送入口把文本写进 ChatInput 的 v-model */
const draft = ref('')

/** 助手「正在生成」状态：等待期间禁止重复发送 */
const pending = ref(false)

const inputRef = ref<InstanceType<typeof ChatInput> | null>(null)
const scrollRef = ref<HTMLElement | null>(null)

const messages = computed(() => conversationStore.messages)
const showWelcome = computed(() => !conversationStore.hasMessages)

/**
 * 真实请求的中断控制。
 *
 * 有了异步请求之后，「正在生成」不再只是一个定时器：如果用户清空会话、
 * 或在请求飞行中离开页面，回调再往 store 写消息就会把脏数据插到新上下文里。
 * 因此保留一个 controller，在两个位置主动取消（见下）。
 */
let inFlightController: AbortController | null = null

/** 消息变化后滚到底部（含占位回复出现的那一次） */
async function scrollToBottom() {
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => messages.value.length, scrollToBottom)
watch(pending, scrollToBottom)

/** 本地占位回复（非考试问题走这条，行为与 F2 完全一致） */
function replyWithMock(text: string) {
  window.setTimeout(() => {
    conversationStore.addMessage('assistant', buildMockReply(text), true)
    pending.value = false
    inputRef.value?.focus()
  }, MOCK_REPLY_DELAY_MS)
}

/**
 * 真实考试查询。
 *
 * 完成信号刻意**观察 assistant 消息数量变化**，而不是直接 `await` 编排函数：
 * 这样即便将来在同一个会话里并行发起了多个请求，也只会由「自己那一条」
 * assistant 消息的写入来解除 pending，不会互相把对方的等待状态清掉。
 */
function replyWithExams() {
  const controller = new AbortController()
  inFlightController = controller

  const beforeCount = conversationStore.messages.filter(
    (message) => message.role === 'assistant',
  ).length

  const stopWatch = watch(
    () =>
      conversationStore.messages.filter((message) => message.role === 'assistant').length >
      beforeCount,
    (arrived) => {
      if (!arrived) return
      stopWatch()
      pending.value = false
      nextTick(() => inputRef.value?.focus())
    },
  )

  void runExamQuery(controller.signal).then((payload) => {
    // 被取消（清空会话 / 离开页面）时不上屏，避免污染新上下文
    if (!shouldRenderReply(payload)) return
    conversationStore.appendMessage(toAssistantMessage(payload))
  })
}

/**
 * 真实课程表查询。
 *
 * 收尾机制与 `replyWithExams` 完全一致（观察 assistant 消息数量变化来解除
 * pending，理由同上），差异只有两点：**入口带显式的周次参数**、
 * 调用的是课程编排层。周次由 `handleSend` 从用户输入抽取后传入：
 * 数字 = 只查该周，undefined = 整学期。
 */
function replyWithCourses(week: number | undefined) {
  const controller = new AbortController()
  inFlightController = controller

  const beforeCount = conversationStore.messages.filter(
    (message) => message.role === 'assistant',
  ).length

  const stopWatch = watch(
    () =>
      conversationStore.messages.filter((message) => message.role === 'assistant').length >
      beforeCount,
    (arrived) => {
      if (!arrived) return
      stopWatch()
      pending.value = false
      nextTick(() => inputRef.value?.focus())
    },
  )

  void runCourseQuery(week, controller.signal).then((payload) => {
    if (!shouldRenderCourseReply(payload)) return
    conversationStore.appendMessage(toCourseAssistantMessage(payload))
  })
}

/**
 * 成绩学期的「任务槽位」：上一轮成绩问题没带学期、正在等用户补。
 *
 * 这是前端本地的最小实现（Python Agent 的 `task_slot` 不动）：
 * 槽位只在本组件存活，且**不渲染任何 UI**——它的存在只影响
 * `handleSend` 对下一句输入的解释。新建 / 清空会话时一并复位，
 * 避免把「上一场对话欠的学期」带进新上下文。
 */
let pendingScoreSemester = false

/**
 * F4 的 Agent 会话标识（**只在本组件实例存活期内有效**）。
 *
 * 生命周期规则（刻意从简：不用全局变量、不做持久化）：
 * - 第一次调用 Agent 时**不传** threadId，由 Agent 自动生成并在响应里回传；
 * - 拿到之后存在这里，之后每一次成绩相关的 Agent 请求都原样带回，
 *   Agent 的 checkpointer 据此加载同一会话状态，「追问学期 → 补齐 → 查询」
 *   这个多轮流程才能跨请求接上；
 * - 新建 / 清空会话时复位为 ``null``：那是另一个上下文，
 *   不该继承上一场的会话状态（否则 Agent 会把上一个会话的待办槽位认成本轮的）。
 */
let agentThreadId: string | null = null

/**
 * 「Agent 正在等用户补学期」的本地槽位。
 *
 * 与 `pendingScoreSemester`（F3 槽位）同形但**不同链路**：F3 槽位在前端解析学期，
 * 本槽位只记录「上一轮 Agent 反问了学期」，本轮输入**原样转发**给 Agent 解释——
 * 「上学期 / 本学期」这类相对说法要靠 Agent 的教务日历，前端没有日历，猜了就是编造。
 * 只在本组件存活，不渲染任何 UI；新建 / 清空会话时一并复位。
 */
let pendingAgentSemester = false

/**
 * 请假流程的「任务槽位」：正在收集请假信息（startTime / endTime / reason）。
 *
 * 与成绩学期槽位同理：槽位只在本组件存活，影响 `handleSend` 对下一句输入的解释。
 * `null` 表示不在请假流程中；非 null 表示已收集到的部分信息。
 */
let pendingLeaveData: LeaveCollectedData | null = null

/**
 * 回一句学期追问（不发任何请求、不打 pending 长等待）。
 *
 * 与真实查询的区别：追问是本地同步产生的，写完立即解除 pending，
 * 用户补学期后由 `handleSend` 的槽位分支发起真正请求。
 */
function askScoreSemester() {
  conversationStore.addMessage('assistant', SCORE_SEMESTER_FOLLOWUP)
  pending.value = false
  inputRef.value?.focus()
}

/**
 * 真实成绩查询。
 *
 * 收尾机制与 `replyWithExams` / `replyWithCourses` 完全一致（观察 assistant
 * 消息数量变化来解除 pending），差异只有一点：**入口带显式的学期参数**——
 * 字符串 = 只查该学期；undefined = 用户明确选择「全部」，查所有学期。
 */
function replyWithScores(semester: string | undefined) {
  const controller = new AbortController()
  inFlightController = controller

  const beforeCount = conversationStore.messages.filter(
    (message) => message.role === 'assistant',
  ).length

  const stopWatch = watch(
    () =>
      conversationStore.messages.filter((message) => message.role === 'assistant').length >
      beforeCount,
    (arrived) => {
      if (!arrived) return
      stopWatch()
      pending.value = false
      nextTick(() => inputRef.value?.focus())
    },
  )

  void runScoreQuery(semester, controller.signal).then((payload) => {
    if (!shouldRenderScoreReply(payload)) return
    conversationStore.appendMessage(toScoreAssistantMessage(payload))
  })
}

/**
 * F3 成绩分支的原始分流逻辑（**原样保留**，F4 起兼任 Agent 的回退链路）。
 *
 * 规则未改动：明确写了学期就按学期查、明确说「全部」就查全部，
 * 两者都没有就只回一句追问并挂上槽位（不猜学期）。
 * 抽成函数只是为了被两处复用（成绩主分支的「全部」例外、Agent 失败后的回退），
 * 行为与 F3-3 完全一致。
 */
function dispatchF3Score(text: string) {
  const semester = parseSemester(text)
  if (semester !== undefined) {
    replyWithScores(semester)
    return
  }
  if (wantsAllSemesters(text)) {
    replyWithScores(undefined)
    return
  }
  pendingScoreSemester = true
  askScoreSemester()
}

/** 当前会话里 assistant 消息条数（真实链路的统一完成信号） */
function countAssistantMessages(): number {
  return conversationStore.messages.filter((message) => message.role === 'assistant').length
}

/**
 * F4 成绩主链路：优先走 Agent，失败时回退 F3 直连链路。
 *
 * 完成信号仍是「assistant 消息数量变化」，但这里有个不同：**由谁写下这条消息
 * 是不确定的**——Agent 成功时是 Agent 自己，失败回退时是 F3 链路。
 * 因此 Agent 失败后**不在这里解除 pending**，交给 F3 那一步自己的完成信号解除；
 * 否则 loading 会先消失、而结果还没到（用户会以为请求没反应）。
 *
 * 失败必须可区分：分类来自 `runAgentScoreChat` 的 `failureKind`
 * （network / http / malformed / timeout），文案来自 api 层
 * （「无法连接 Agent 服务」/「服务返回异常（HTTP xxx）」/「返回内容不是合法
 * JSON」/「请求超时」）。分类只记进开发期控制台——屏幕上出现的是 F3 的
 * **真实结果或真实失败**，绝不会把 Agent 的失败当作成功渲染。
 */
function replyWithAgentScore(text: string) {
  const controller = new AbortController()
  inFlightController = controller

  const beforeCount = countAssistantMessages()
  const stopWatch = watch(
    () => countAssistantMessages() > beforeCount,
    (arrived) => {
      if (!arrived) return
      stopWatch()
      pending.value = false
      nextTick(() => inputRef.value?.focus())
    },
  )

  void runAgentScoreChat(text, agentThreadId ?? undefined, controller.signal).then(
    ({ reply, threadId, failureKind }) => {
      if (threadId) agentThreadId = threadId

      // 被取消（清空会话 / 离开页面）：空载荷不上屏，也不触发回退
      if (!reply.error && !shouldRenderAgentReply(reply)) {
        stopWatch()
        pending.value = false
        return
      }

      // —— Agent 成功：只展示 Agent 的回复 ——
      if (!reply.error) {
        conversationStore.appendMessage(toAgentAssistantMessage(reply))
        return
      }

      // —— Agent 失败：按类型记录，然后回退 F3 直连链路把同一件事做完 ——
      // 回退意味着这条会话改由 F3 承担：Agent 的学期槽位就地放弃，
      // 后面若 F3 再追问学期，由 F3 自己的槽位（pendingScoreSemester）接手。
      pendingAgentSemester = false
      if (import.meta.env.DEV) {
        console.warn('[F4] Agent 成绩链路失败，回退 F3 直连链路', {
          kind: failureKind ?? 'unknown',
          message: reply.content,
        })
      }
      stopWatch()
      dispatchF3Score(text)
    },
  )
}

/**
 * 真实请假提交。
 *
 * 收尾机制与 `replyWithExams` / `replyWithCourses` / `replyWithScores` 完全一致。
 */
function replyWithLeave(data: LeaveCollectedData) {
  const controller = new AbortController()
  inFlightController = controller

  const beforeCount = conversationStore.messages.filter(
    (message) => message.role === 'assistant',
  ).length

  const stopWatch = watch(
    () =>
      conversationStore.messages.filter((message) => message.role === 'assistant').length >
      beforeCount,
    (arrived) => {
      if (!arrived) return
      stopWatch()
      pending.value = false
      pendingLeaveData = null
      nextTick(() => inputRef.value?.focus())
    },
  )

  void submitLeave(data, controller.signal).then((payload) => {
    if (!shouldRenderLeaveReply(payload)) return
    conversationStore.appendMessage(toLeaveAssistantMessage(payload))
  })
}

/**
 * 切换会话即换上下文：Agent 会话标识与「等学期」槽位一起复位。
 *
 * 为什么需要这个 watch：会话除了在「新对话 / 清空」按钮里变，还能被侧边栏
 * 直接切走（`store.selectConversation`）。漏掉那条路径的话，新会话的第一句
 * 「帮我查成绩」会带着**上一场**的 threadId 发出去，Agent 会把上一场没做完的
 * 任务当成这一轮的语境——那是最隐蔽的一类多轮串味。
 * 清空会话不改变 currentConversationId，所以它那两条路径仍显式复位（见下）。
 *
 * **为什么必须忽略 `previous === null` 这一次变化**（实测踩到的坑）：
 * 本条会话的第一条消息会走 `store.addMessage` → `upsertCurrentConversation`，
 * 也就是「没有会话 → 新建一个会话」，`currentConversationId` 会从 null 变成新 id。
 * 那不是用户在切会话，而是**本条消息自己开了会话**；若在这里复位，
 * 刚在 `handleSend` 里挂上的「等 Agent 反问学期」槽位会被立刻清掉，
 * 第二轮补学期就会掉进 mock 回复分支（第一版实现正是如此，靠真实浏览器验证才发现）。
 */
watch(
  () => conversationStore.currentConversationId,
  (current, previous) => {
    if (previous === null || previous === undefined) return // 首条消息自建会话，不算切换
    if (current === previous) return
    agentThreadId = null
    pendingAgentSemester = false
  },
)

function handleSend(text: string) {
  if (pending.value) return

  conversationStore.addMessage('user', text)
  pending.value = true

  // —— 成绩槽位：上一轮在等学期时，本轮输入优先按「补学期」解释 ——
  // 补上了（或明确说「全部」）就查；还在问成绩但没给学期就再问一次；
  // 问的是别的事就放弃槽位、走正常分流（不让一个旧追问劫持新问题）。
  if (pendingScoreSemester) {
    const followedUpSemester = parseSemester(text)
    if (followedUpSemester !== undefined) {
      pendingScoreSemester = false
      replyWithScores(followedUpSemester)
      return
    }
    if (wantsAllSemesters(text)) {
      pendingScoreSemester = false
      replyWithScores(undefined)
      return
    }
    if (isScoreQuery(text)) {
      askScoreSemester()
      return
    }
    pendingScoreSemester = false
  }

  // —— 请假槽位：上一轮在等补充信息时，本轮输入优先按「补请假信息」解释 ——
  if (pendingLeaveData) {
    const updated = parseLeaveInput(text, pendingLeaveData)
    if (isLeaveComplete(updated)) {
      pendingLeaveData = null
      replyWithLeave(updated)
      return
    }
    if (isLeaveQuery(text)) {
      pendingLeaveData = updated
      const followup = getLeaveFollowup(updated)
      if (followup) {
        conversationStore.addMessage('assistant', followup)
        pending.value = false
        inputRef.value?.focus()
        return
      }
    }
    pendingLeaveData = null
  }

  // —— Agent 学期槽位：上一轮 Agent 反问了学期，本轮输入优先按「补学期」解释 ——
  // 与 F3 槽位的区别：前端**不解析**这句话，原样转发给 Agent——
  // 「上学期 / 本学期」要靠 Agent 的教务日历，前端猜了就是编造。
  // 只有「用户明显换去问别的事」（考试 / 课程 / 请假）才放弃等待、走正常分流，
  // 否则「一个旧追问劫持新问题」的老毛病就会回来。
  //
  // 位置说明：刻意排在 F3 的**两个槽位分支之后**、各业务分流之前。
  // 一是「槽位都在分流之前处理」这条既有结构不变；
  // 二是 F3 分支之间的相对顺序（成绩 → 请假 → 考试 → 课程）分毫未动——
  // F3-4 的源码契约用例正是按首次出现位置核对这个顺序的，这里不能去动它。
  if (pendingAgentSemester) {
    if (isExamQuery(text) || isCourseQuery(text) || isLeaveQuery(text)) {
      pendingAgentSemester = false
    } else if (wantsAllSemesters(text)) {
      // 「全部学期」交给 F3（与成绩主分支同一取舍，理由见那里）
      pendingAgentSemester = false
      dispatchF3Score(text)
      return
    } else {
      // 写出了具体学期就解除等待；否则（例如用户又问了一遍成绩）继续等
      if (parseSemester(text) !== undefined) pendingAgentSemester = false
      replyWithAgentScore(text)
      return
    }
  }

  // —— 成绩分流在最前：「考试成绩 / 期末成绩」问的是分数，不是考场安排 ——
  // F4：优先走 Agent（Agent 负责意图识别、学期追问、工具调用与自然语言生成），
  // 失败时由 replyWithAgentScore 回退 F3 直连链路（dispatchF3Score）。
  if (isScoreQuery(text)) {
    // 例外：「全部 / 所有学期」直接走 F3。Agent 的学期槽位只认「写出来的学年 +
    // 学期号」（见 app/agent/slots.py），没有「全部学期」这个说法；而 F3 的接口层
    // 本来就支持「不带 semester = 查全部」。交给能回答这条问题的链路，
    // 比让 Agent 反复追问更诚实——这条分支不新增行为，就是 F3 原本的做法。
    if (wantsAllSemesters(text)) {
      dispatchF3Score(text)
      return
    }
    // 没写出具体学期（也没说「全部」）→ Agent 大概率会反问学期：挂上槽位，
    // 下一轮输入才会被当成「补学期」交给同一个 Agent 会话
    pendingAgentSemester = parseSemester(text) === undefined
    replyWithAgentScore(text)
    return
  }

  // —— 请假分流：用户要请假，先尝试从输入里解析日期和事由 ——
  if (isLeaveQuery(text)) {
    const data = parseLeaveInput(text, {})
    if (isLeaveComplete(data)) {
      replyWithLeave(data)
      return
    }
    pendingLeaveData = data
    const followup = getLeaveFollowup(data)
    if (followup) {
      conversationStore.addMessage('assistant', followup)
      pending.value = false
      inputRef.value?.focus()
      return
    }
    pendingLeaveData = null
  }

  if (isExamQuery(text)) {
    replyWithExams()
    return
  }

  // 课程问题：周次从输入里抽（「查看第3周课程」→ 3），抽不到就是整学期；
  // 先判考试、再判课程，保证 F3-1 的考试分流行为分毫不改
  if (isCourseQuery(text)) {
    replyWithCourses(parseCourseWeek(text))
    return
  }

  replyWithMock(text)
}

/** 点击示例问题：只填入输入框并聚焦，不发送、不调用后端 */
function handleSelectPrompt(question: string) {
  draft.value = question
  nextTick(() => inputRef.value?.focus())
}

function handleNewConversation() {
  pendingScoreSemester = false
  pendingLeaveData = null
  // 新会话 = 新上下文：不复用上一场的 Agent 会话（`createConversation` 可能
  // 复用「当前空会话」，那时 currentConversationId 不变、上面那个 watch 不会触发）
  agentThreadId = null
  pendingAgentSemester = false
  conversationStore.createConversation()
  inputRef.value?.focus()
}

/** 清空会话前先取消飞行中的请求，防止迟到的响应写进已清空的对话 */
function handleClearConversation() {
  inFlightController?.abort()
  inFlightController = null
  pendingScoreSemester = false
  pendingLeaveData = null
  agentThreadId = null
  pendingAgentSemester = false
  pending.value = false
  conversationStore.clearCurrentConversation()
  inputRef.value?.focus()
}

onMounted(() => {
  inputRef.value?.focus()
})

onBeforeUnmount(() => {
  inFlightController?.abort()
  inFlightController = null
})

/**
 * 调试入口（仅开发环境）：把 store 挂到 window，方便在控制台验证
 * 「新建对话 / 添加消息 / 重置」这些本轮被要求的操作。
 * F3 追加：把真实查询入口也挂上去，便于不点按钮就验证接口链路。
 * F4 追加：Agent 成绩链路入口 + **当前 threadId 的读取口**——
 *   threadId 是本组件内部的 ``let``，从外部读不到，而「第二轮是否复用同一个
 *   threadId」正是 F4 要验证的关键事实，因此开一个只读的取值函数（仍非全局变量，
 *   只是把已有的局部状态暴露给开发期控制台）。
 */
if (import.meta.env.DEV) {
  const debugTarget = window as unknown as Record<string, unknown>
  debugTarget.__campusFlowConversationStore = conversationStore
  debugTarget.__campusFlowExamQuery = runExamQuery
  debugTarget.__campusFlowCourseQuery = runCourseQuery
  debugTarget.__campusFlowScoreQuery = runScoreQuery
  debugTarget.__campusFlowLeaveQuery = submitLeave
  debugTarget.__campusFlowAgentScoreChat = runAgentScoreChat
  debugTarget.__campusFlowAgentThreadId = () => agentThreadId
}
</script>

<template>
  <div class="chat-page">
    <AppSidebar :open="sidebarOpen" @close="sidebarOpen = false" />

    <!-- 窄屏遮罩：点击关闭抽屉 -->
    <div
      v-if="sidebarOpen"
      class="chat-page__scrim"
      aria-hidden="true"
      @click="sidebarOpen = false"
    ></div>

    <main class="chat-page__main">
      <header class="chat-page__topbar">
        <button
          type="button"
          class="chat-page__menu"
          aria-label="打开对话列表"
          @click="sidebarOpen = !sidebarOpen"
        >
          ☰
        </button>
        <span class="chat-page__topbar-title">
          {{ conversationStore.currentConversation?.title ?? '新对话' }}
        </span>
        <button
          type="button"
          class="chat-page__clear"
          :disabled="showWelcome"
          @click="handleClearConversation"
        >
          清空
        </button>
      </header>

      <div ref="scrollRef" class="chat-page__scroll">
        <WelcomePanel v-if="showWelcome" @select-prompt="handleSelectPrompt" />

        <div v-else class="chat-page__messages">
          <ChatMessage
            v-for="message in messages"
            :key="message.id"
            :role="message.role"
            :content="message.content"
            :mock="message.mock"
            :error="message.error"
          />
          <ChatMessage v-if="pending" role="assistant" content="" pending />
        </div>
      </div>

      <ChatInput
        ref="inputRef"
        v-model="draft"
        :disabled="pending"
        @send="handleSend"
      />
    </main>
  </div>
</template>

<style scoped>
.chat-page {
  position: relative;
  display: flex;
  height: 100%;
  min-width: 0;
  overflow: hidden;
}

.chat-page__main {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-width: 0;
  height: 100%;
  background: var(--cf-bg);
}

.chat-page__topbar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.625rem 1.5rem;
  border-bottom: 1px solid var(--cf-border);
}

.chat-page__menu {
  display: none;
  padding: 0.125rem 0.375rem;
  font: inherit;
  font-size: 0.9375rem;
  color: var(--cf-text-muted);
  background: transparent;
  border: 1px solid var(--cf-border);
  border-radius: 7px;
  cursor: pointer;
}

.chat-page__topbar-title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 0.8125rem;
  color: var(--cf-text-muted);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.chat-page__clear {
  flex: 0 0 auto;
  padding: 0.1875rem 0.5rem;
  font: inherit;
  font-size: 0.75rem;
  color: var(--cf-text-muted);
  background: transparent;
  border: 1px solid var(--cf-border);
  border-radius: 7px;
  cursor: pointer;
}

.chat-page__clear:disabled {
  color: var(--cf-text-subtle);
  cursor: not-allowed;
  opacity: 0.6;
}

.chat-page__scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  /* 超长内容只在本区域内换行/滚动，页面本身不出现横向滚动条 */
  overflow-x: hidden;
}

.chat-page__messages {
  padding: 1.5rem;
}

.chat-page__scrim {
  position: absolute;
  inset: 0;
  z-index: 10;
  background: transparent;
}

@media (max-width: 860px) {
  .chat-page__menu {
    display: inline-block;
  }

  .chat-page__topbar {
    padding: 0.625rem 1rem;
  }

  .chat-page__messages {
    padding: 1rem;
  }
}
</style>
