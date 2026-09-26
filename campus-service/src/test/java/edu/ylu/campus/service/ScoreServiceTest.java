package edu.ylu.campus.service;

import edu.ylu.campus.dto.ScoreResponse;
import edu.ylu.campus.entity.Score;
import edu.ylu.campus.repository.ScoreRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.lang.reflect.RecordComponent;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * 成绩服务层测试：验证业务查询语义（含学期分派）与实体 -> DTO 的转换。
 *
 * <p>刻意断言**数值类型**而不只是取值：成绩与学分是给模型比较与加权的数据，
 * 一旦在 DTO 里被字符串化（例如 {@code "92.0"}），模型就得自己转回数字。
 * 这里守住「分数/学分经 DTO 出来仍是数值」。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class ScoreServiceTest {

    private static final String STUDENT = "student-001";
    private static final String CURRENT_SEMESTER = "2025-2026-1";
    private static final String LAST_SEMESTER = "2024-2025-2";

    @Autowired
    private ScoreService scoreService;

    @Autowired
    private ScoreRepository repository;

    private void seed() {
        repository.saveAll(List.of(
                new Score(STUDENT, CURRENT_SEMESTER, "数据结构", 92.0, 4.0),
                new Score(STUDENT, CURRENT_SEMESTER, "计算机网络", 88.0, 3.0),
                new Score(STUDENT, LAST_SEMESTER, "高等数学", 84.0, 5.0),
                new Score("student-002", CURRENT_SEMESTER, "大学物理", 90.0, 4.0)
        ));
        repository.flush();
    }

    @Test
    @DisplayName("返回 DTO 列表：四个字段、分数学分保持数值、按学期从新到旧")
    void returnsDtosWithNumericFields() {
        seed();

        List<ScoreResponse> scores = scoreService.findScoresByStudent(STUDENT, null);

        assertThat(scores).hasSize(3);
        assertThat(scores)
                .extracting(ScoreResponse::semester)
                .containsExactly(CURRENT_SEMESTER, CURRENT_SEMESTER, LAST_SEMESTER);

        ScoreResponse first = scores.get(0);
        assertThat(first.course()).isEqualTo("数据结构");
        assertThat(first.score()).isEqualByComparingTo("92.0");
        assertThat(first.credit()).isEqualByComparingTo("4.0");
    }

    @Test
    @DisplayName("semester 为 null 时返回全部成绩；给出 semester 时只返回该学期")
    void semesterIsOptionalAndDispatchesToTheRightQuery() {
        seed();

        List<ScoreResponse> whole = scoreService.findScoresByStudent(STUDENT, null);
        List<ScoreResponse> current = scoreService.findScoresByStudent(STUDENT, CURRENT_SEMESTER);
        List<ScoreResponse> last = scoreService.findScoresByStudent(STUDENT, LAST_SEMESTER);

        assertThat(whole).hasSize(3);
        assertThat(current).extracting(ScoreResponse::course).containsExactlyInAnyOrder("数据结构", "计算机网络");
        assertThat(last).extracting(ScoreResponse::course).containsExactly("高等数学");
        // 过滤确实生效，而不是原样全返
        assertThat(current).extracting(ScoreResponse::course).doesNotContain("高等数学");
    }

    @Test
    @DisplayName("空白学期按「未提供」处理，而不是拿空串去精确匹配")
    void blankSemesterIsTreatedAsNotProvided() {
        seed();

        // ?semester= 这种空值是调用方笔误；若按字面匹配会返回空列表，
        // 被误读成「这学期没有成绩」，比返回全部更容易误导。
        assertThat(scoreService.findScoresByStudent(STUDENT, "")).hasSize(3);
        assertThat(scoreService.findScoresByStudent(STUDENT, "   ")).hasSize(3);
    }

    @Test
    @DisplayName("未知学号 / 未知学期都返回空列表（没出分不是错误）")
    void returnsEmptyListForUnknownStudentOrSemester() {
        seed();

        assertThat(scoreService.findScoresByStudent("student-999", null)).isEmpty();
        assertThat(scoreService.findScoresByStudent("student-999", CURRENT_SEMESTER)).isEmpty();
        assertThat(scoreService.findScoresByStudent(STUDENT, "2023-2024-1")).isEmpty();
    }

    @Test
    @DisplayName("学生之间数据隔离：查 student-002 不会带出 student-001 的成绩")
    void isolatesStudents() {
        seed();

        assertThat(scoreService.findScoresByStudent("student-002", null))
                .extracting(ScoreResponse::course)
                .containsExactly("大学物理");
    }

    @Test
    @DisplayName("对外结构被 DTO 收敛：不暴露 id / studentId / createdAt 等内部字段")
    void dtoExposesOnlyAgentFacingFields() {
        seed();

        ScoreResponse response = scoreService.findScoresByStudent(STUDENT, null).get(0);

        assertThat(ScoreResponse.class.getRecordComponents())
                .extracting(RecordComponent::getName)
                .containsExactly("semester", "course", "score", "credit");
        assertThat(response.toString()).doesNotContain("student-001");
    }
}
