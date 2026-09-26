/**
 * F3 验证脚手架（临时文件，验证完即删）。
 *
 * 目的：不依赖浏览器，直接用 **Vite 的 SSR 模块加载**跑真实的 `src/` 模块，
 * 并通过 `fetch` 打真实 HTTP，验证两条路径：
 *   1. 成功路径：真实接口返回 → 消息内容包含真实考试数据；
 *   2. 失败路径：接口不可用 → 明确失败提示，且**不包含任何考试数据**。
 *
 * 它加载的是真实源码（@/api/exams、@/lib/examFlow），不是复制品，
 * 所以通过即代表前端逻辑本身正确。
 *
 * 用法（不能带 configFile，SSR 下会触发 dep-scan 报错）：
 *   BASE_URL=http://localhost:8080 STUDENT_ID=student-001 node scripts/verify-f3-exam-flow.mjs
 */

import { createServer } from 'vite'

const BASE_URL = process.env.BASE_URL ?? 'http://localhost:8080'
const STUDENT_ID = process.env.STUDENT_ID ?? 'student-001'

// 注入环境变量：Vite 在 SSR 加载时会替换 import.meta.env.VITE_*
process.env.VITE_API_BASE_URL = BASE_URL
process.env.VITE_CAMPUS_STUDENT_ID = STUDENT_ID

const results = []
function check(name, ok, detail = '') {
  results.push({ name, ok })
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ` >> ${detail}` : ''}`)
}

const srcRoot = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')

const server = await createServer({
  root: process.cwd(),
  logLevel: 'error',
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true },
  resolve: { alias: [{ find: '@', replacement: srcRoot }] },
})

try {
  const env = await server.ssrLoadModule('/src/config/env.ts')
  const intent = await server.ssrLoadModule('/src/constants/examIntent.ts')
  const examsApi = await server.ssrLoadModule('/src/api/exams.ts')
  const flow = await server.ssrLoadModule('/src/lib/examFlow.ts')

  console.log('=== F3 exam query chain verification ===')
  console.log(`BaseURL=${env.API_BASE_URL} StudentId=${env.resolveStudentId()} Path=${examsApi.EXAMS_PATH}`)

  console.log('\n--- 1) intent routing ---')
  const q1 = '\u5e2e\u6211\u67e5\u4e00\u4e0b\u8003\u8bd5\u5b89\u6392'
  const q2 = '\u4e0b\u4e2a\u6708\u7684\u8003\u8bd5\u5b89\u6392\u662f\u4ec0\u4e48\uff1f'
  const q3 = '\u6211\u8fd9\u5468\u6709\u54ea\u4e9b\u8bfe\uff1f'
  const q4 = '\u6211\u60f3\u8bf7\u4e8b\u5047\uff0c\u9700\u8981\u63d0\u4f9b\u54ea\u4e9b\u4fe1\u606f\uff1f'
  check('sample "check exam schedule" hits exam intent', intent.isExamQuery(q1))
  check('sample "next month exam schedule" hits exam intent', intent.isExamQuery(q2))
  check('course question does NOT hit exam intent', !intent.isExamQuery(q3))
  check('leave question does NOT hit exam intent', !intent.isExamQuery(q4))

  console.log('\n--- 2) direct API call ---')
  let directExams = null
  let directError = null
  try {
    directExams = await examsApi.fetchExamSchedules({ studentId: env.resolveStudentId() })
    console.log(`HTTP OK, ${directExams.length} item(s)`)
    console.log(`raw = ${JSON.stringify(directExams)}`)
  } catch (e) {
    directError = e
    console.log(`HTTP FAIL: ${e.name} / ${e.message}`)
  }

  console.log('\n--- 3) orchestration layer (the entry the component calls) ---')
  const payload = await flow.runExamQuery()
  console.log(`role=${payload.role} error=${payload.error === true}`)
  console.log(`content:\n${payload.content}`)

  console.log('\n--- 4) assertions ---')
  if (directExams) {
    check(
      'structure is 4 string fields (course/date/time/room)',
      directExams.every(
        (x) =>
          typeof x.course === 'string' &&
          typeof x.date === 'string' &&
          typeof x.time === 'string' &&
          typeof x.room === 'string',
      ),
    )
    check('no leaked entity fields (id/studentId/createdAt/examDate)', directExams.every(
      (x) => !('id' in x) && !('studentId' in x) && !('createdAt' in x) && !('examDate' in x),
    ))
    if (directExams.length > 0) {
      check(
        'every real row appears in the chat text',
        directExams.every(
          (x) =>
            payload.content.includes(x.course) &&
            payload.content.includes(x.room) &&
            payload.content.includes(x.date) &&
            payload.content.includes(x.time),
        ),
      )
      check('no error flag on success', payload.error !== true)
    } else {
      check('empty array handled as normal result (no error flag)', payload.error !== true)
      check('empty array message says no exam', payload.content.includes('\u6ca1\u6709'))
    }
    check('payload is renderable', flow.shouldRenderReply(payload))
  } else {
    check('failure text contains the required wording', payload.content.includes('\u8003\u8bd5\u5b89\u6392\u67e5\u8be2\u5931\u8d25'), payload.content)
    check('failure payload carries error flag', payload.error === true)
    const leaked = ['\u8ba1\u7b97\u673a\u7f51\u7edc', '\u6570\u636e\u7ed3\u6784', 'A302', 'B201', '2026-09-28']
    check('NO fabricated exam data in failure text', !leaked.some((k) => payload.content.includes(k)))
    check('error normalized to ExamApiError', directError?.name === 'ExamApiError', directError?.name)
  }

  const failed = results.filter((r) => !r.ok)
  console.log(`\n=== summary: ${results.length - failed.length}/${results.length} passed ===`)
  process.exitCode = failed.length === 0 ? 0 : 1
} finally {
  await server.close()
}
