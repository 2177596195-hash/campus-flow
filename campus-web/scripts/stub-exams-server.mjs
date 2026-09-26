/**
 * 一次性桩服务（临时文件，验证完即删）。
 *
 * 严格按后端契约返回：`List<ExamScheduleResponse>`，四字段纯字符串数组，
 * 数据取 `campus-service/src/main/resources/data.sql` 中 student-001 的真实种子行。
 * 只用来验证「HTTP 成功 → 真实数据进入聊天区」这条前端路径，
 * **不改后端、不算作真实后端联调**。
 *
 * 用法：node scripts/stub-exams-server.mjs [port]
 */

import { createServer } from 'node:http'

const PORT = Number(process.argv[2] ?? 8099)

// 逐字取自后端 data.sql 的 student-001 两行（date/time 为 DTO 的字符串格式）
const SEED = {
  'student-001': [
    { course: '计算机网络', date: '2026-09-28', time: '14:00', room: 'A302' },
    { course: '数据结构', date: '2026-09-30', time: '19:00', room: 'B201' },
  ],
  'student-002': [{ course: '大学英语', date: '2026-09-29', time: '09:00', room: 'C105' }],
}

const server = createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`)
  if (url.pathname !== '/api/exams') {
    res.writeHead(404).end('not found')
    return
  }

  const studentId = url.searchParams.get('studentId')
  if (!studentId) {
    // 与后端一致：缺少 studentId 返回 400
    res.writeHead(400).end('missing studentId')
    return
  }

  res.writeHead(200, { 'Content-Type': 'application/json;charset=UTF-8' })
  res.end(JSON.stringify(SEED[studentId] ?? []))
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`stub listening on http://127.0.0.1:${PORT}`)
})
