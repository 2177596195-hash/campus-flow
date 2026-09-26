package edu.ylu.campus.dto;

import edu.ylu.campus.entity.Score;

import java.math.BigDecimal;

/**
 * 成绩对外响应体。
 *
 * <p>为什么需要 DTO 而不是直接返回实体：对外契约与持久化模型是两件事。
 * 实体带 {@code id} / {@code studentId} / {@code createdAt} 等内部字段，
 * 直接暴露会让数据库结构变化直接冲击 API 消费方（Python 侧的 Tool）。
 * 这里显式只暴露 Agent 需要的四个字段。
 *
 * <p>字段名与 Python 侧 {@code get_score} 的返回结构完全一致，因此 JSON 可以
 * 近乎零转换地进入 ToolMessage。字段顺序即对外顺序，与 Python 侧的
 * {@code SCORE_FIELDS} 一一对应：
 *
 * <pre>semester / course / score / credit</pre>
 *
 * <p>{@code score} 与 {@code credit} 保持**数值类型**（{@link BigDecimal} 经
 * Jackson 序列化为 JSON 数字），不做字符串化——模型要能直接比较大小与做加权，
 * 拿到 {@code "92.0"} 这样的字符串还得自己转。
 *
 * @param semester 学期，形如 {@code 2025-2026-1}
 * @param course   课程名称
 * @param score    成绩分数
 * @param credit   学分
 */
public record ScoreResponse(
        String semester,
        String course,
        BigDecimal score,
        BigDecimal credit
) {

    /**
     * 实体 -> 响应体的转换。
     */
    public static ScoreResponse from(Score entity) {
        return new ScoreResponse(
                entity.getSemester(),
                entity.getCourse(),
                entity.getScore(),
                entity.getCredit()
        );
    }
}
