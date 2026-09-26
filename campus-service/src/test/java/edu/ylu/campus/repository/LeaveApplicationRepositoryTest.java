package edu.ylu.campus.repository;

import edu.ylu.campus.entity.LeaveApplication;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 请假仓储测试：验证写入语义、默认状态、排序规则，以及「这是一张追加型表」。
 *
 * <p>与 {@code ScoreRepositoryTest} 同一套做法：{@code @SpringBootTest} + 真实 MySQL
 * + 事务回滚，全项目只创建一个 Spring 上下文。
 *
 * <p>排序用例用 {@link JdbcTemplate} **显式写入 created_at**：{@code save} 走
 * {@code @PrePersist} 取的是 {@code now()}，同一毫秒内的两条记录谁前谁后不确定，
 * 把它写进断言会变成偶发失败。由测试控制时间戳，断言的才是查询本身的 ORDER BY。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class LeaveApplicationRepositoryTest {

    private static final String STUDENT = "student-001";
    private static final String OTHER_STUDENT = "student-002";

    @Autowired
    private LeaveApplicationRepository repository;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    @Test
    @DisplayName("写入即回读：字段完整，状态默认 PENDING，提交时间与主键自动生成")
    void savesAndReadsBackWithDefaults() {
        LocalDateTime start = LocalDateTime.of(2026, 10, 12, 8, 0);
        LocalDateTime end = LocalDateTime.of(2026, 10, 14, 18, 0);

        LeaveApplication saved = repository.saveAndFlush(
                new LeaveApplication(STUDENT, start, end, "感冒发烧需就医")
        );

        assertThat(saved.getId()).isNotNull();
        assertThat(saved.getStudentId()).isEqualTo(STUDENT);
        assertThat(saved.getStartTime()).isEqualTo(start);
        assertThat(saved.getEndTime()).isEqualTo(end);
        assertThat(saved.getReason()).isEqualTo("感冒发烧需就医");
        // 初始状态由 @PrePersist 兜底：调用方无从指定，也就不可能漏填
        assertThat(saved.getStatus()).isEqualTo(LeaveApplication.STATUS_PENDING);
        assertThat(saved.getCreatedAt()).isNotNull();
    }

    @Test
    @DisplayName("按学号查询：只返回该学生的申请，且按提交时间从新到旧")
    void findsByStudentNewestFirst() {
        insertRaw(STUDENT, "2026-10-12 08:00:00", "2026-10-12 18:00:00", "事由A", "2026-10-01 09:00:00");
        insertRaw(STUDENT, "2026-10-20 08:00:00", "2026-10-20 18:00:00", "事由B", "2026-10-03 09:00:00");
        insertRaw(STUDENT, "2026-10-25 08:00:00", "2026-10-25 18:00:00", "事由C", "2026-10-02 09:00:00");
        insertRaw(OTHER_STUDENT, "2026-10-15 08:00:00", "2026-10-15 18:00:00", "他人事由", "2026-10-04 09:00:00");

        List<LeaveApplication> mine = repository.findByStudentIdOrderByCreatedAtDesc(STUDENT);

        assertThat(mine).extracting(LeaveApplication::getReason)
                .containsExactly("事由B", "事由C", "事由A");
        assertThat(mine).extracting(LeaveApplication::getReason).doesNotContain("他人事由");
        assertThat(repository.findByStudentIdOrderByCreatedAtDesc(OTHER_STUDENT))
                .extracting(LeaveApplication::getReason)
                .containsExactly("他人事由");
    }

    @Test
    @DisplayName("未知学号返回空列表而不是异常")
    void returnsEmptyListForUnknownStudent() {
        assertThat(repository.findByStudentIdOrderByCreatedAtDesc("student-999")).isEmpty();
    }

    @Test
    @DisplayName("追加语义：同一学生同一时段重复提交是两条申请（刻意不设唯一键）")
    void duplicateApplicationsAreAllowed() {
        // 反向守卫：若有人给这张表补上 (student_id, start_time, end_time) 唯一键，
        // 「用户重提一次」就会变成 500。请假是流水，不是事实表，这里必须允许重复。
        LocalDateTime start = LocalDateTime.of(2026, 10, 12, 8, 0);
        LocalDateTime end = LocalDateTime.of(2026, 10, 14, 18, 0);

        LeaveApplication first = repository.saveAndFlush(
                new LeaveApplication(STUDENT, start, end, "重提测试")
        );
        LeaveApplication second = repository.saveAndFlush(
                new LeaveApplication(STUDENT, start, end, "重提测试")
        );

        assertThat(first.getId()).isNotEqualTo(second.getId());
        assertThat(repository.findByStudentIdOrderByCreatedAtDesc(STUDENT)).hasSize(2);
    }

    private void insertRaw(
            String studentId, String start, String end, String reason, String createdAt
    ) {
        jdbcTemplate.update(
                "INSERT INTO leave_application "
                        + "(student_id, start_time, end_time, reason, status, created_at) "
                        + "VALUES (?, ?, ?, ?, 'PENDING', ?)",
                studentId, start, end, reason, createdAt
        );
    }
}
