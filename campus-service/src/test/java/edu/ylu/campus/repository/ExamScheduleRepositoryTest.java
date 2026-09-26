package edu.ylu.campus.repository;

import edu.ylu.campus.entity.ExamSchedule;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.time.LocalTime;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 仓储层测试：直接验证派生查询的语义与表约束。
 *
 * <p>为什么用 {@code @SpringBootTest} 而不是 {@code @DataJpaTest}：
 * 本项目所有 Java 测试共用同一份上下文配置（真实 MySQL + 事务回滚），
 * 这样只会创建**一个** Spring 上下文，避免 JPA 切片测试与全量测试各自
 * 对同一张表执行 DDL 而相互破坏。查询行为本身仍只经过 JPA 层。
 *
 * <p>前置条件：见 {@code src/test/resources/application-test.yml}。
 * 每个用例都在事务内执行并回滚，因此用例之间互不影响、也不会留下脏数据。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class ExamScheduleRepositoryTest {

    private static final String STUDENT = "student-001";

    @Autowired
    private ExamScheduleRepository repository;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    private void seed() {
        repository.saveAll(List.of(
                new ExamSchedule(STUDENT, "计算机网络", LocalDate.of(2026, 9, 28), LocalTime.of(14, 0), "A302"),
                new ExamSchedule(STUDENT, "数据结构", LocalDate.of(2026, 9, 30), LocalTime.of(19, 0), "B201"),
                new ExamSchedule("student-002", "大学英语", LocalDate.of(2026, 9, 29), LocalTime.of(9, 0), "C105")
        ));
        repository.flush();
    }

    @Test
    @DisplayName("按学号查询：只返回该学生的考试，且按日期与开考时间升序")
    void findsExamsOfGivenStudentOrderedByTime() {
        seed();

        List<ExamSchedule> exams = repository.findByStudentIdOrderByExamDateAscExamTimeAsc(STUDENT);

        assertThat(exams).hasSize(2);
        assertThat(exams)
                .extracting(ExamSchedule::getCourse)
                .containsExactly("计算机网络", "数据结构");
        assertThat(exams)
                .extracting(ExamSchedule::getExamDate)
                .containsExactly(LocalDate.of(2026, 9, 28), LocalDate.of(2026, 9, 30));
    }

    @Test
    @DisplayName("未知学号返回空列表而不是异常")
    void returnsEmptyListForUnknownStudent() {
        seed();

        assertThat(repository.findByStudentIdOrderByExamDateAscExamTimeAsc("student-999")).isEmpty();
    }

    @Test
    @DisplayName("按学号隔离：不会把其它学生的考试带出来")
    void doesNotLeakOtherStudentsExams() {
        seed();

        List<ExamSchedule> other = repository.findByStudentIdOrderByExamDateAscExamTimeAsc("student-002");

        assertThat(other).hasSize(1);
        assertThat(other.get(0).getCourse()).isEqualTo("大学英语");
        assertThat(other)
                .extracting(ExamSchedule::getCourse)
                .doesNotContain("计算机网络", "数据结构");
    }

    @Test
    @DisplayName("排序由查询完成：乱序写入后仍按考试时间升序返回")
    void orderingIsDoneByTheQueryNotByInsertionOrder() {
        repository.save(new ExamSchedule(STUDENT, "数据结构", LocalDate.of(2026, 9, 30), LocalTime.of(19, 0), "B201"));
        repository.save(new ExamSchedule(STUDENT, "计算机网络", LocalDate.of(2026, 9, 28), LocalTime.of(14, 0), "A302"));
        repository.flush();

        assertThat(repository.findByStudentIdOrderByExamDateAscExamTimeAsc(STUDENT))
                .extracting(ExamSchedule::getCourse)
                .containsExactly("计算机网络", "数据结构");
    }

    @Test
    @DisplayName("自然唯一键生效：重复的 (学号, 科目, 日期) 被数据库拒绝")
    void naturalUniqueKeyIsEnforced() {
        // 该约束是「种子脚本可用 INSERT IGNORE 写成幂等」的前提，
        // 因此这里直接验证它确实存在于表结构里。
        seed();

        assertThatThrownBy(() -> jdbcTemplate.update(
                "INSERT INTO exam_schedule (student_id, course, exam_date, exam_time, room) "
                        + "VALUES (?, ?, ?, ?, ?)",
                STUDENT, "计算机网络", LocalDate.of(2026, 9, 28), LocalTime.of(14, 0), "A302"
        )).isInstanceOf(DuplicateKeyException.class);
    }
}
