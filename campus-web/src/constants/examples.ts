/**
 * 欢迎页示例问题。
 *
 * 只是一份静态文案清单：点击后把文本填进输入框，**不触发任何请求**。
 * 放在独立模块里是为了让「产品能做什么」这件事只在一处维护。
 */

export interface ExamplePrompt {
  /** 唯一 key */
  id: string
  /** 卡片标题（短，2~6 字） */
  title: string
  /** 点击后填入输入框的完整问句 */
  question: string
}

export const EXAMPLE_PROMPTS: readonly ExamplePrompt[] = [
  {
    id: 'score',
    title: '查询成绩',
    question: '帮我查一下上学期的成绩',
  },
  {
    id: 'course',
    title: '查询课表',
    question: '我这周有哪些课？',
  },
  {
    id: 'exam',
    title: '查询考试安排',
    question: '下个月的考试安排是什么？',
  },
  {
    id: 'rule',
    title: '查询校园规则',
    question: '图书馆的开放时间和借阅规则是怎样的？',
  },
  {
    id: 'leave',
    title: '办理请假',
    question: '我想请事假，需要提供哪些信息？',
  },
]

/** 能力概览文案（欢迎页与侧边栏共用一处维护） */
export const CAPABILITIES: readonly string[] = [
  '查询成绩',
  '查询课表',
  '查询考试安排',
  '查询校园规则',
  '办理请假',
]
