package edu.ylu.campus.dto;

/**
 * 请假申请提交的请求体。
 *
 * <p>字段名与 Python 侧 {@code CampusApiClient.submit_leave_application} 发出的
 * JSON 键一致（{@code studentId} / {@code startTime} / {@code endTime} / {@code reason}）。
 *
 * <p><b>时间为何收 {@link String} 而不是 {@code LocalDateTime}</b>
 * <br>时间来自模型或用户的口语化输入，写法不止一种（{@code 2026-10-12T08:00}、
 * {@code 2026-10-12 08:00}、甚至只有日期）。若在这里声明成 {@code LocalDateTime}，
 * Jackson 反序列化失败的报错发生在框架层、措辞面向开发者，用户只会看到
 * 「请求体格式错误」这类无法据以修正的提示。收字符串、由 Service 统一解析，
 * 才能对「哪一种写法不合法」给出**可读的、能照着改**的中文原因——
 * 这与 Python 侧「异常消息即产品文案」的一贯做法对齐。
 *
 * <p>字段全部可为 {@code null}：缺失字段需要被 Service 判成「缺失」并返回 400，
 * 而不是在进方法之前就抛一个框架异常（那样拿不到可控的错误文案）。
 *
 * @param studentId 学号（必填）
 * @param startTime 请假开始时间（必填），形如 {@code 2026-10-12 08:00}
 * @param endTime   请假结束时间（必填），不得早于 {@code startTime}
 * @param reason    请假事由（必填，非空白）
 */
public record LeaveApplicationRequest(
        String studentId,
        String startTime,
        String endTime,
        String reason
) {
}
