package edu.ylu.campus.repository;

import edu.ylu.campus.entity.Score;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.math.BigDecimal;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.tuple;

/**
 * 成绩仓储测试：直接验证派生查询的语义、排序规则与表约束。
 *
 * <p>与 {@code ExamScheduleRepositoryTest} / {@code CourseScheduleRepositoryTest}
 * 同一套做法与理由：用 {@code @SpringBootTest} + 真实 MySQL + 事务回滚，
 * 全项目只创建一个 Spring 上下文。
 *
 * <p>排序用例刻意用 **ASCII 课程名**：中文课程名的排序依赖数据库排序规则
 * （utf8mb4_general_ci 按码位比较），把「中文按什么顺序排」写进断言等于把
 * 数据库排序规则钉死在测试里。这里要守的是**查询带 ORDER BY 且语义是
 * 「学期从新到旧、同学期按课程名」**，用 ASCII 就能精确验证这一点。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class ScoreRepositoryTest {

    private static final String STUDENT = "student-001";
    private static final String OTHER_STUDENT = "student-002";

    private static final String CURRENT_SEMESTER = "2025-2026-1";
    private static final String LAST_SEMESTER = "2024-2025-2";

    @Autowired
    private ScoreRepository repository;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /**
     * 种子数据刻意覆盖四种情形：同一学期多门课、跨两个学期、学分含小数、
     * 另一个学生的成绩（用于验证隔离）。
     */
    private void seed() {
        repository.saveAll(List.of(
                new Score(STUDENT, CURRENT_SEMESTER, "数据结构", 92.0, 4.0),
                new Score(STUDENT, CURRENT_SEMESTER, "计算机网络", 88.0, 3.0),
                new Score(STUDENT, CURRENT_SEMESTER, "操作系统", 95.0, 3.5),
                new Score(STUDENT, LAST_SEMESTER, "高等数学", 84.0, 5.0),
                new Score(STUDENT, LAST_SEMESTER, "大学英语", 86.0, 2.0),
                new Score(OTHER_STUDENT, CURRENT_SEMESTER, "大学物理", 90.0, 4.0)
        ));
        repository.flush();
    }

    @Test
    @DisplayName("按学号查询：只返回该学生的成绩，学期从新到旧")
    void findsScoresOfGivenStudentNewestSemesterFirst() {
        seed();

        List<Score> scores = repository.findByStudentIdOrderBySemesterDescCourseAsc(STUDENT);

        assertThat(scores).hasSize(5);
        // 学期是「从新到旧」：2025-2026-1 的三门前置，2024-2025-2 的两门在后
        assertThat(scores)
                .extracting(Score::getSemester)
                .containsExactly(
                        CURRENT_SEMESTER, CURRENT_SEMESTER, CURRENT_SEMESTER,
                        LAST_SEMESTER, LAST_SEMESTER
                );
        assertThat(scores)
                .extracting(Score::getCourse)
                .doesNotContain("大学物理");
    }

    @Test
    @DisplayName("排序由查询完成：乱序写入后仍按「学期从新到旧、同学期按课程名」返回")
    void orderingIsDoneByTheQueryNotByInsertionOrder() {
        // 课程名用 ASCII，使「同学期内的排序」与数据库排序规则无关，可精确断言
        repository.save(new Score(STUDENT, LAST_SEMESTER, "Algorithms", 80.0, 3.0));
        repository.save(new Score(STUDENT, CURRENT_SEMESTER, "Compilers", 81.0, 3.0));
        repository.save(new Score(STUDENT, CURRENT_SEMESTER, "Algorithms", 82.0, 3.0));
        repository.save(new Score(STUDENT, CURRENT_SEMESTER, "Databases", 83.0, 3.0));
        repository.flush();

        assertThat(repository.findByStudentIdOrderBySemesterDescCourseAsc(STUDENT))
                .extracting(Score::getCourse, Score::getSemester)
                .containsExactly(
                        tuple("Algorithms", CURRENT_SEMESTER),
                        tuple("Compilers", CURRENT_SEMESTER),
                        tuple("Databases", CURRENT_SEMESTER),
                        tuple("Algorithms", LAST_SEMESTER)
                );
    }

    @Test
    @DisplayName("按学期查询：只返回该学期的成绩，且同学期内按课程名升序")
    void filtersBySemester() {
        seed();

        List<Score> current = repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, CURRENT_SEMESTER);
        List<Score> last = repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, LAST_SEMESTER);

        assertThat(current).hasSize(3);
        assertThat(last).extracting(Score::getCourse).containsExactlyInAnyOrder("高等数学", "大学英语");
        // 学期过滤是真的在过滤，而不是原样全返
        assertThat(current)
                .extracting(Score::getCourse)
                .doesNotContain("高等数学", "大学英语", "大学物理");
        assertThat(last)
                .extracting(Score::getCourse)
                .doesNotContain("数据结构", "计算机网络", "操作系统");
    }

    @Test
    @DisplayName("学期是精确匹配：不做前缀匹配，2025 不等于 2025-2026-1")
    void semesterMatchIsExact() {
        seed();

        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, "2025")).isEmpty();
        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, "2025-2026-2")).isEmpty();
        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, CURRENT_SEMESTER))
                .isNotEmpty();
    }

    @Test
    @DisplayName("学号 + 学期组合过滤：两个条件同时生效")
    void combinesStudentAndSemesterFilters() {
        seed();

        List<Score> other = repository.findByStudentIdAndSemesterOrderByCourseAsc(OTHER_STUDENT, CURRENT_SEMESTER);

        assertThat(other).extracting(Score::getCourse).containsExactly("大学物理");
        assertThat(other).extracting(Score::getCourse).doesNotContain("数据结构", "计算机网络");
        // 该学生该学期没有成绩 → 空列表（不是异常）
        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc(OTHER_STUDENT, LAST_SEMESTER)).isEmpty();
    }

    @Test
    @DisplayName("未知学号返回空列表而不是异常")
    void returnsEmptyListForUnknownStudent() {
        seed();

        assertThat(repository.findByStudentIdOrderBySemesterDescCourseAsc("student-999")).isEmpty();
        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc("student-999", CURRENT_SEMESTER))
                .isEmpty();
    }

    @Test
    @DisplayName("按学号隔离：不会把其它学生的成绩带出来")
    void doesNotLeakOtherStudentsScores() {
        seed();

        List<Score> other = repository.findByStudentIdOrderBySemesterDescCourseAsc(OTHER_STUDENT);

        assertThat(other).hasSize(1);
        assertThat(other.get(0).getCourse()).isEqualTo("大学物理");
        assertThat(other)
                .extracting(Score::getCourse)
                .doesNotContain("数据结构", "计算机网络", "操作系统", "高等数学", "大学英语");
    }

    @Test
    @DisplayName("分数与学分的精度由 DECIMAL 保证：91.5 读出仍是 91.5")
    void decimalPrecisionIsPreserved() {
        // 用二进制浮点存 91.5 恰好无损，但 0.1 这类值会漂；这里验证的是
        // 「列类型确实是 DECIMAL、JPA 映射确实是 BigDecimal」这条链路。
        repository.save(new Score(STUDENT, CURRENT_SEMESTER, "数值方法", new BigDecimal("91.50"), new BigDecimal("2.5")));
        repository.flush();

        Score saved = repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, CURRENT_SEMESTER).get(0);

        assertThat(saved.getScore()).isEqualByComparingTo("91.5");
        assertThat(saved.getCredit()).isEqualByComparingTo("2.5");
        // 不是二进制浮点：BigDecimal 才能无损承载十进制小数
        assertThat(saved.getScore()).isInstanceOf(BigDecimal.class);
    }

    @Test
    @DisplayName("自然唯一键生效：重复的 (学号, 学期, 课程) 被数据库拒绝")
    void naturalUniqueKeyIsEnforced() {
        // 该约束是「种子脚本可用 INSERT IGNORE 写成幂等」的前提，
        // 因此这里直接验证它确实存在于表结构里。
        seed();

        assertThatThrownBy(() -> jdbcTemplate.update(
                "INSERT INTO score (student_id, semester, course, score, credit) VALUES (?, ?, ?, ?, ?)",
                STUDENT, CURRENT_SEMESTER, "数据结构", new BigDecimal("60.0"), new BigDecimal("4.0")
        )).isInstanceOf(DuplicateKeyException.class);
    }

    @Test
    @DisplayName("同一门课在不同学期是两条记录（重修），唯一键不误伤")
    void sameCourseInDifferentSemestersIsAllowed() {
        // 反向守卫：若把唯一键误设成 (student_id, course)，
        // 「同一门课跨学期重修」这种常态就会被数据库拒绝。
        seed();

        repository.save(new Score(STUDENT, LAST_SEMESTER, "数据结构", 61.0, 4.0));
        repository.flush();

        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, LAST_SEMESTER))
                .extracting(Score::getCourse)
                .contains("数据结构");
        assertThat(repository.findByStudentIdAndSemesterOrderByCourseAsc(STUDENT, CURRENT_SEMESTER))
                .extracting(Score::getCourse)
                .contains("数据结构");
    }
}
