package edu.ylu.campus.dto;

import edu.ylu.campus.entity.LeaveApplication;

import java.time.LocalDateTime;

/**
 * 请假申请对外响应体。
 *
 * <p>与 {@link ScoreResponse} 同一套理由：对外契约与持久化模型是两件事，
 * 显式列出 Agent 需要的字段，而不是直接暴露实体。
 *
 * <p>字段顺序即对外顺序，与 Python 侧 {@code leave_contract.LEAVE_RESULT_FIELDS}
 * 一一对应：
 *
 * <pre>id / studentId / startTime / endTime / reason / status / createdAt</pre>
 *
 * <p><b>这里与前三个 DTO 不同：把 {@code id} 也暴露出去</b>
 * <br>查询类接口的 {@code id} 是纯内部字段（模型答「数据结构 92 分」时用不到），
 * 因此刻意隐藏。请假则相反——{@code id} 就是**申请编号**，用户要拿它去跟进审核
 * 进度，是这条链路上最值得复述的信息，必须返回。
 *
 * <p>时间字段是 {@link LocalDateTime}，由 Jackson 序列化成 ISO 8601 字符串
 * （如 {@code "2026-10-12T08:00:00"}），Python 侧按文本归一化后交给模型，
 * 因此「日期是给人看的字符串」这条约定在跨语言边界上保持一致。
 *
 * @param id        申请编号
 * @param studentId 学号
 * @param startTime 请假开始时间
 * @param endTime   请假结束时间
 * @param reason    请假事由
 * @param status    审核状态，提交后为 {@code PENDING}
 * @param createdAt 提交时间
 */
public record LeaveApplicationResponse(
        Long id,
        String studentId,
        LocalDateTime startTime,
        LocalDateTime endTime,
        String reason,
        String status,
        LocalDateTime createdAt
) {

    /**
     * 实体 -> 响应体的转换。
     */
    public static LeaveApplicationResponse from(LeaveApplication entity) {
        return new LeaveApplicationResponse(
                entity.getId(),
                entity.getStudentId(),
                entity.getStartTime(),
                entity.getEndTime(),
                entity.getReason(),
                entity.getStatus(),
                entity.getCreatedAt()
        );
    }
}
