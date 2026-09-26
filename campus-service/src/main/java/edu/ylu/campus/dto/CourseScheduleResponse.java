package edu.ylu.campus.dto;

import edu.ylu.campus.entity.CourseSchedule;

/**
 * 课程安排对外响应体。
 *
 * <p>为什么需要 DTO 而不是直接返回实体：对外契约与持久化模型是两件事。
 * 实体带 {@code id} / {@code studentId} / {@code weekStart} / {@code createdAt}
 * 等内部字段，直接暴露会让数据库结构变化直接冲击 API 消费方（Python 侧的 Tool）。
 * 这里显式只暴露 Agent 需要的六个字段。
 *
 * <p>字段名与 Python 侧 {@code get_course_schedule} 的返回结构完全一致，
 * 因此 JSON 可以近乎零转换地进入 ToolMessage。其中两个字段是**渲染**出来的：
 *
 * <ul>
 *   <li>{@code weekday}：库里存 1..7 序号，对外渲染为「周一」…「周日」，
 *       避免把「哪种写法表示星期几」这件事泄漏给调用方；</li>
 *   <li>{@code weeks}：库里存 {@code week_start}/{@code week_end} 区间，
 *       对外渲染为「1-16周」，与接入真实服务之前 Python 侧 mock 的输出格式一致，
 *       因此 Agent 的回答措辞不会因为这次改造而变化。</li>
 * </ul>
 *
 * @param course  课程名称
 * @param teacher 任课教师
 * @param weekday 星期，形如 {@code 周一}
 * @param time    上课时段，形如 {@code 08:00-09:40}
 * @param room    教室
 * @param weeks   上课周次区间，形如 {@code 1-16周}
 */
public record CourseScheduleResponse(
        String course,
        String teacher,
        String weekday,
        String time,
        String room,
        String weeks
) {

    /**
     * 星期序号 -> 中文标签。索引即序号，下标 0 留空以便直接用
     * {@code WEEKDAY_LABELS[weekday]} 取值，不用做 {@code -1} 换算。
     */
    private static final String[] WEEKDAY_LABELS = {
            "", "周一", "周二", "周三", "周四", "周五", "周六", "周日"
    };

    /**
     * 实体 -> 响应体的转换。
     */
    public static CourseScheduleResponse from(CourseSchedule entity) {
        return new CourseScheduleResponse(
                entity.getCourse(),
                entity.getTeacher(),
                weekdayLabel(entity.getWeekday()),
                entity.getClassTime(),
                entity.getRoom(),
                "%d-%d周".formatted(entity.getWeekStart(), entity.getWeekEnd())
        );
    }

    /**
     * 序号 -> 中文星期标签。
     *
     * <p>越界时抛异常而不是回退成「周一」：库里出现 0 或 8 说明数据写错了，
     * 静默回退会把一条错误数据伪装成正常课表，比直接失败更难排查。
     */
    private static String weekdayLabel(int weekday) {
        if (weekday < 1 || weekday >= WEEKDAY_LABELS.length) {
            throw new IllegalStateException("课程数据存在非法的星期序号: " + weekday);
        }
        return WEEKDAY_LABELS[weekday];
    }
}
