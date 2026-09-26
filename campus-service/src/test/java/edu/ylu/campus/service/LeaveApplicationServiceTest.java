package edu.ylu.campus.service;

import edu.ylu.campus.dto.LeaveApplicationRequest;
import edu.ylu.campus.dto.LeaveApplicationResponse;
import edu.ylu.campus.entity.LeaveApplication;
import edu.ylu.campus.repository.LeaveApplicationRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 请假服务层测试：验证入参校验、时间解析、时间先后关系与落库语义。
 *
 * <p>这里的每个失败用例都对应「模型可能给出的写法」——所以断言的不只是「抛异常」，
 * 还有**错误消息里有可读的中文原因**：它会经 REST 原样回到模型，是模型改参重试的
 * 唯一依据（与 Python 侧「异常消息即产品文案」的约定一致）。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class LeaveApplicationServiceTest {

    private static final String STUDENT = "student-001";

    @Autowired
    private LeaveApplicationService service;

    @Autowired
    private LeaveApplicationRepository repository;

    private static LeaveApplicationRequest request(String start, String end, String reason) {
        return new LeaveApplicationRequest(STUDENT, start, end, reason);
    }

    @Test
    @DisplayName("提交成功：返回申请编号与 PENDING 状态，并把记录真的写进库里")
    void submitsAndPersistsPendingApplication() {
        LeaveApplicationResponse response = service.submit(
                request("2026-10-12 08:00", "2026-10-14 18:00", "感冒发烧需就医")
        );

        assertThat(response.id()).isNotNull();
        assertThat(response.studentId()).isEqualTo(STUDENT);
        assertThat(response.startTime()).isEqualTo(LocalDateTime.of(2026, 10, 12, 8, 0));
        assertThat(response.endTime()).isEqualTo(LocalDateTime.of(2026, 10, 14, 18, 0));
        assertThat(response.reason()).isEqualTo("感冒发烧需就医");
        // 初始状态不由调用方决定，也不该被调用方改写
        assertThat(response.status()).isEqualTo(LeaveApplication.STATUS_PENDING);
        assertThat(response.createdAt()).isNotNull();

        List<LeaveApplication> persisted =
                repository.findByStudentIdOrderByCreatedAtDesc(STUDENT);
        assertThat(persisted).hasSize(1);
        assertThat(persisted.get(0).getId()).isEqualTo(response.id());
    }

    @Test
    @DisplayName("时间两种写法都接受：带空格与带 T，等价解析")
    void acceptsBothSpaceAndIsoSeparator() {
        LeaveApplicationResponse spaced = service.submit(
                request("2026-10-12 08:00", "2026-10-12 18:00", "空格写法")
        );
        LeaveApplicationResponse isoT = service.submit(
                request("2026-10-12T08:00", "2026-10-12T18:00", "T 写法")
        );

        assertThat(spaced.startTime()).isEqualTo(isoT.startTime());
        assertThat(spaced.endTime()).isEqualTo(isoT.endTime());
    }

    @Test
    @DisplayName("只给日期时按当天零点处理（容忍手工联调少写时刻）")
    void acceptsDateOnly() {
        LeaveApplicationResponse response = service.submit(
                request("2026-10-12", "2026-10-13", "只给日期")
        );

        assertThat(response.startTime()).isEqualTo(LocalDateTime.of(2026, 10, 12, 0, 0));
        assertThat(response.endTime()).isEqualTo(LocalDateTime.of(2026, 10, 13, 0, 0));
    }

    @Test
    @DisplayName("缺失 / 空白的必填项一律被拒，且原因里点明是哪一项")
    void rejectsMissingOrBlankRequiredFields() {
        assertThatThrownBy(() -> service.submit(
                new LeaveApplicationRequest(null, "2026-10-12 08:00", "2026-10-12 18:00", "事由")
        )).isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("studentId");

        assertThatThrownBy(() -> service.submit(request(null, "2026-10-12 18:00", "事由")))
                .isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("开始时间");

        assertThatThrownBy(() -> service.submit(request("2026-10-12 08:00", "  ", "事由")))
                .isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("结束时间");

        // 纯空白事由算缺失：一条没有事由的申请没有意义
        assertThatThrownBy(() -> service.submit(request("2026-10-12 08:00", "2026-10-12 18:00", "   ")))
                .isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("事由");
    }

    @Test
    @DisplayName("时间格式无法识别时被拒，原因里给出正确格式")
    void rejectsUnparsableTime() {
        assertThatThrownBy(() -> service.submit(
                request("明天早上", "2026-10-12 18:00", "事由")
        )).isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("2026-10-12 08:00");

        assertThatThrownBy(() -> service.submit(
                request("2026-10-12 08:00", "2026/10/12 18:00", "事由")
        )).isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("结束时间");
    }

    @Test
    @DisplayName("结束时间早于开始时间被拒（否则会安静地存出一条无意义申请）")
    void rejectsEndBeforeStart() {
        assertThatThrownBy(() -> service.submit(
                request("2026-10-14 18:00", "2026-10-12 08:00", "时间倒置")
        )).isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("不能早于");

        // 起止相同是允许的（例如请假半天、只到一个时刻）
        assertThat(service.submit(
                request("2026-10-12 08:00", "2026-10-12 08:00", "同一时刻")
        ).status()).isEqualTo(LeaveApplication.STATUS_PENDING);
    }

    @Test
    @DisplayName("超长事由被拒（避免以 500 的形式撞数据库列长度）")
    void rejectsOverlongReason() {
        String tooLong = "很".repeat(LeaveApplication.REASON_MAX_LENGTH + 1);

        assertThatThrownBy(() -> service.submit(
                request("2026-10-12 08:00", "2026-10-12 18:00", tooLong)
        )).isInstanceOf(InvalidLeaveApplicationException.class)
                .hasMessageContaining("过长");
    }

    @Test
    @DisplayName("不同的申请各自获得独立编号（追加语义，不去重）")
    void eachSubmissionGetsItsOwnId() {
        LeaveApplicationResponse first = service.submit(
                request("2026-10-12 08:00", "2026-10-12 18:00", "第一次")
        );
        LeaveApplicationResponse second = service.submit(
                request("2026-10-12 08:00", "2026-10-12 18:00", "第一次")
        );

        assertThat(first.id()).isNotEqualTo(second.id());
        assertThat(repository.findByStudentIdOrderByCreatedAtDesc(STUDENT)).hasSize(2);
    }
}
