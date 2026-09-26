package edu.ylu.campus.service;

import edu.ylu.campus.dto.ExamScheduleResponse;
import edu.ylu.campus.entity.ExamSchedule;
import edu.ylu.campus.repository.ExamScheduleRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.time.LocalTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 服务层测试：验证业务查询语义与实体 → DTO 的转换。
 *
 * <p>刻意断言**输出格式**（{@code yyyy-MM-dd} / {@code HH:mm}）而不只是字段个数，
 * 因为这三个字符串格式正是 Python 侧 Tool 依赖的对外契约：
 * 一旦这里改了格式，Agent 的展示就会跟着变。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class ExamServiceTest {

    private static final String STUDENT = "student-001";

    @Autowired
    private ExamService examService;

    @Autowired
    private ExamScheduleRepository repository;

    private void seed() {
        repository.saveAll(List.of(
                new ExamSchedule(STUDENT, "计算机网络", LocalDate.of(2026, 9, 28), LocalTime.of(14, 0), "A302"),
                new ExamSchedule(STUDENT, "数据结构", LocalDate.of(2026, 9, 30), LocalTime.of(19, 0), "B201")
        ));
        repository.flush();
    }

    @Test
    @DisplayName("返回 DTO 列表，日期与时间已格式化为稳定字符串")
    void returnsDtosWithStableDateFormat() {
        seed();

        List<ExamScheduleResponse> exams = examService.findExamsByStudent(STUDENT);

        assertThat(exams).hasSize(2);
        ExamScheduleResponse first = exams.get(0);
        assertThat(first.course()).isEqualTo("计算机网络");
        assertThat(first.date()).isEqualTo("2026-09-28");   // ISO-8601 日期
        assertThat(first.time()).isEqualTo("14:00");        // 只到分钟，无秒
        assertThat(first.room()).isEqualTo("A302");
    }

    @Test
    @DisplayName("未知学号返回空列表（无考试不是错误）")
    void returnsEmptyListForUnknownStudent() {
        seed();

        assertThat(examService.findExamsByStudent("student-999")).isEmpty();
    }

    @Test
    @DisplayName("对外结构被 DTO 收敛：不暴露 id / studentId / createdAt 等内部字段")
    void dtoExposesOnlyAgentFacingFields() {
        seed();

        ExamScheduleResponse response = examService.findExamsByStudent(STUDENT).get(0);

        assertThat(ExamScheduleResponse.class.getRecordComponents())
                .extracting(component -> component.getName())
                .containsExactly("course", "date", "time", "room");
        assertThat(response.toString()).doesNotContain("student-001");
    }
}
