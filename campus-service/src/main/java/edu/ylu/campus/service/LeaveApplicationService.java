package edu.ylu.campus.service;

import edu.ylu.campus.dto.LeaveApplicationRequest;
import edu.ylu.campus.dto.LeaveApplicationResponse;
import edu.ylu.campus.entity.LeaveApplication;
import edu.ylu.campus.repository.LeaveApplicationRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.format.DateTimeParseException;

/**
 * 请假申请业务：校验入参、落库、返回新建的申请记录。
 *
 * <p>业务规则（入参校验、时间解析、时间先后关系、初始状态的归属）全部收敛在
 * Service 层：Controller 不直接操作 Repository，Repository 也不承载业务逻辑。
 *
 * <p><b>为什么校验放在 Service 而不是 Controller</b>
 * <br>「什么算一条合法的请假申请」是业务语义（而非协议细节），它必须对
 * **所有**入站路径一致生效——今天的调用方是 Controller，将来若出现批量导入或
 * 管理端补录，同一套规则不该被复制第二遍。Controller 只负责把失败翻译成 400。
 *
 * <p><b>为什么校验放在 Repository 之外、由本层显式做</b>
 * <br>Java 侧刻意没有引入 Bean Validation 依赖（见 {@code pom.xml} 的说明），
 * 且列长度、时间顺序这类约束若只靠数据库报错，会以 500 的形式暴露给调用方——
 * 而它其实是「调用方传错了」这类可预期的 4xx。因此这里显式校验并抛
 * {@link InvalidLeaveApplicationException}。
 */
@Service
public class LeaveApplicationService {

    private final LeaveApplicationRepository repository;

    public LeaveApplicationService(LeaveApplicationRepository repository) {
        this.repository = repository;
    }

    /**
     * 校验并提交一条请假申请。
     *
     * <p>写入是**追加**语义：每次成功调用都会新增一条记录并返回其申请编号，
     * 不做「同一时段去重」——重复提交是两条申请，这是刻意的（见实体类说明）。
     *
     * @param request 提交的入参
     * @return 新建的申请记录（含申请编号与初始状态 {@code PENDING}）
     * @throws InvalidLeaveApplicationException 任一必填项缺失 / 空白 / 超长，
     *         时间格式无法识别，或结束时间早于开始时间
     */
    @Transactional
    public LeaveApplicationResponse submit(LeaveApplicationRequest request) {
        if (request == null) {
            throw new InvalidLeaveApplicationException("提交内容不能为空。");
        }

        String studentId = requireText(
                request.studentId(), "studentId", LeaveApplication.STUDENT_ID_MAX_LENGTH
        );
        String reason = requireText(
                request.reason(), "请假事由", LeaveApplication.REASON_MAX_LENGTH
        );
        LocalDateTime startTime = requireTime(request.startTime(), "请假开始时间");
        LocalDateTime endTime = requireTime(request.endTime(), "请假结束时间");

        if (endTime.isBefore(startTime)) {
            // 时间倒置是最容易被模型犯下的错（把结束时间写到开始时间之前），
            // 且它不会让任何一列违反约束、会安静地存成一条无意义的申请，
            // 因此必须在写入前拦下。
            throw new InvalidLeaveApplicationException("请假结束时间不能早于开始时间。");
        }

        LeaveApplication saved = repository.save(
                new LeaveApplication(studentId, startTime, endTime, reason)
        );
        return LeaveApplicationResponse.from(saved);
    }

    /**
     * 取必填文本并校验长度；缺失 / 空白 / 超长一律视为不合法。
     *
     * @param field 字段的中文名，用于拼出可读的错误原因
     */
    private static String requireText(String value, String field, int maxLength) {
        String text = value == null ? "" : value.trim();
        if (text.isEmpty()) {
            throw new InvalidLeaveApplicationException(field + "不能为空。");
        }
        if (text.length() > maxLength) {
            // 提前拦下，避免超长内容让数据库抛错、最终以 500 的形式返回——
            // 那是「调用方传错了」，就该是 400。
            throw new InvalidLeaveApplicationException(
                    field + "过长（最多 " + maxLength + " 个字符）。"
            );
        }
        return text;
    }

    /**
     * 解析时间入参，统一成 {@link LocalDateTime}。
     *
     * <p>接受两种常见写法：日期与时刻都给的（{@code 2026-10-12T08:00} /
     * {@code 2026-10-12 08:00}），以及只给日期的（{@code 2026-10-12}，按当天零点）。
     * 前者与 Python 侧 {@code leave_contract.normalize_datetime} 的输出一致，
     * 后者是为了容忍直连 REST 的手工调用（如联调时的 curl）少写时刻。
     *
     * <p>无法识别时抛**可读中文**的 {@link InvalidLeaveApplicationException}——
     * 与 Python 侧「异常消息即产品文案」一致，原因会经 REST 回到模型，
     * 让它照着格式重新提问，而不是笼统地报「参数错误」。
     */
    private static LocalDateTime requireTime(String value, String field) {
        String text = value == null ? "" : value.trim();
        if (text.isEmpty()) {
            throw new InvalidLeaveApplicationException(field + "不能为空。");
        }
        // 把日期与时刻之间的空白统一成 ISO 8601 的 'T'：这样空格写法与 'T' 写法
        // 走同一条解析路径，不必为「宽不宽容」写两套判断。
        String normalized = text.replaceAll("\\s+", "T");
        try {
            return LocalDateTime.parse(normalized);
        } catch (DateTimeParseException notADateTime) {
            try {
                return LocalDate.parse(normalized).atStartOfDay();
            } catch (DateTimeParseException notADate) {
                throw new InvalidLeaveApplicationException(
                        field + "格式无法识别：" + value + "，请使用形如 2026-10-12 08:00 的格式。"
                );
            }
        }
    }
}
