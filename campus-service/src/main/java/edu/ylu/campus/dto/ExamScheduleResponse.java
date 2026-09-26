package edu.ylu.campus.dto;

import edu.ylu.campus.entity.ExamSchedule;

import java.time.format.DateTimeFormatter;

/**
 * 考试安排对外响应体。
 *
 * <p>为什么需要 DTO 而不是直接返回实体：对外契约与持久化模型是两件事。
 * 实体带 {@code id} / {@code studentId} / {@code createdAt} 等内部字段，
 * 直接暴露会让数据库结构变化直接冲击 API 消费方（Python 侧的 Tool）。
 * 这里显式只暴露 Agent 需要的四个字段，并把日期/时间渲染为稳定字符串格式。
 *
 * <p>字段名与 Python 侧 {@code get_exam_schedule} 的返回结构保持一致，
 * 因此 JSON 可以近乎零转换地进入 ToolMessage。
 *
 * @param course 考试科目
 * @param date   考试日期，格式 {@code yyyy-MM-dd}
 * @param time   开考时间，格式 {@code HH:mm}
 * @param room   考场
 */
public record ExamScheduleResponse(String course, String date, String time, String room) {

    /** 时间格式化：对外只保留到分钟，避免暴露无意义的秒。 */
    private static final DateTimeFormatter TIME_FORMAT = DateTimeFormatter.ofPattern("HH:mm");

    /**
     * 实体 → 响应体的转换。
     */
    public static ExamScheduleResponse from(ExamSchedule entity) {
        return new ExamScheduleResponse(
                entity.getCourse(),
                entity.getExamDate().toString(),
                entity.getExamTime().format(TIME_FORMAT),
                entity.getRoom()
        );
    }
}
