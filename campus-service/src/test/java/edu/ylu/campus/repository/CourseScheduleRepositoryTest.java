package edu.ylu.campus.repository;

import edu.ylu.campus.entity.CourseSchedule;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 课程仓储测试：直接验证派生查询的语义与表约束。
 *
 * <p>与 {@code ExamScheduleRepositoryTest} 同一套做法与理由：用
 * {@code @SpringBootTest} + 真实 MySQL + 事务回滚，全项目只创建一个 Spring 上下文。
 *
 * <p>前置条件：见 {@code src/test/resources/application-test.yml}。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class CourseScheduleRepositoryTest {

    private static final String STUDENT = "student-001";
    private static final String OTHER_STUDENT = "student-002";

    @Autowired
    private CourseScheduleRepository repository;

    @Autowired
    private JdbcTemplate jdbcTemplate;

    /**
     * 种子数据刻意覆盖四种情形：同一门课排两天（数据结构周一/周四 之外这里用
     * 周一 + 周三）、同一天多门课（用于验证同一天内按上课时间排序）、
     * 周次区间不同、中途开课（数据库原理 5-16 周）。
     */
    private void seed() {
        repository.saveAll(List.of(
                new CourseSchedule(STUDENT, "数据结构", "李明", 1, "08:00-09:40", "A302", 1, 16),
                new CourseSchedule(STUDENT, "计算机网络", "王芳", 1, "14:00-15:40", "B201", 1, 16),
                new CourseSchedule(STUDENT, "高等数学", "张伟", 2, "08:00-09:40", "C105", 1, 18),
                new CourseSchedule(STUDENT, "大学英语", "刘洋", 3, "10:00-11:40", "D202", 1, 16),
                new CourseSchedule(STUDENT, "数据库原理", "赵磊", 5, "08:00-09:40", "B305", 5, 16),
                new CourseSchedule(OTHER_STUDENT, "大学物理", "周敏", 2, "10:00-11:40", "F203", 1, 16)
        ));
        repository.flush();
    }

    @Test
    @DisplayName("按学号查询：只返回该学生的课程，且按星期、上课时间升序")
    void findsCoursesOfGivenStudentOrderedByWeekdayThenTime() {
        seed();

        List<CourseSchedule> courses = repository.findByStudentIdOrderByWeekdayAscClassTimeAsc(STUDENT);

        assertThat(courses)
                .extracting(CourseSchedule::getCourse)
                .containsExactly("数据结构", "计算机网络", "高等数学", "大学英语", "数据库原理");
        assertThat(courses)
                .extracting(CourseSchedule::getWeekday)
                .containsExactly(1, 1, 2, 3, 5);
        // 同一天内按上课时间升序：周一两门课里 08:00 的排在 14:00 前面
        assertThat(courses.get(0).getClassTime()).isEqualTo("08:00-09:40");
        assertThat(courses.get(1).getClassTime()).isEqualTo("14:00-15:40");
    }

    @Test
    @DisplayName("按周查询：只返回覆盖该周的课程（week_start <= week <= week_end）")
    void filtersByWeek() {
        seed();

        List<CourseSchedule> week3 = repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        STUDENT, 3, 3);

        // 数据库原理第 5 周才开课，第 3 周查不到——这正是「按周过滤真的生效」的证据，
        // 而不是只看条数变化
        assertThat(week3).extracting(CourseSchedule::getCourse).doesNotContain("数据库原理");
        assertThat(week3).hasSize(4);

        List<CourseSchedule> week8 = repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        STUDENT, 8, 8);

        assertThat(week8).extracting(CourseSchedule::getCourse).contains("数据库原理");
        assertThat(week8).hasSize(5);
    }

    @Test
    @DisplayName("周次上下界都是闭区间：边界周次命中，越界一周即落空")
    void weekBoundsAreInclusive() {
        seed();

        assertThat(weekCourses(4)).extracting(CourseSchedule::getCourse).doesNotContain("数据库原理");
        assertThat(weekCourses(5)).extracting(CourseSchedule::getCourse).contains("数据库原理");
        assertThat(weekCourses(16)).extracting(CourseSchedule::getCourse).contains("数据库原理");
        // 1-16 周的课在第 17 周已经结课
        assertThat(weekCourses(17)).extracting(CourseSchedule::getCourse).doesNotContain("数据结构");
        // 高等数学开到第 18 周，比其它课多两周
        assertThat(weekCourses(17)).extracting(CourseSchedule::getCourse).contains("高等数学");
        assertThat(weekCourses(18)).extracting(CourseSchedule::getCourse).contains("高等数学");
        assertThat(weekCourses(19)).isEmpty();
    }

    @Test
    @DisplayName("学号 + 周次组合过滤：两个条件同时生效")
    void combinesStudentAndWeekFilters() {
        seed();

        List<CourseSchedule> own = repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        STUDENT, 3, 3);
        List<CourseSchedule> other = repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        OTHER_STUDENT, 3, 3);

        assertThat(other).extracting(CourseSchedule::getCourse).containsExactly("大学物理");
        assertThat(other).extracting(CourseSchedule::getCourse).doesNotContain("数据结构", "高等数学");
        assertThat(own).extracting(CourseSchedule::getCourse).doesNotContain("大学物理");

        // 学号查得到、但该周无课 → 空列表（不是异常）：
        // student-002 的课只开到第 16 周
        assertThat(repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        OTHER_STUDENT, 17, 17))
                .isEmpty();
    }

    @Test
    @DisplayName("未知学号返回空列表而不是异常")
    void returnsEmptyListForUnknownStudent() {
        seed();

        assertThat(repository.findByStudentIdOrderByWeekdayAscClassTimeAsc("student-999")).isEmpty();
        assertThat(repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        "student-999", 3, 3))
                .isEmpty();
    }

    @Test
    @DisplayName("按学号隔离：不会把其它学生的课程带出来")
    void doesNotLeakOtherStudentsCourses() {
        seed();

        List<CourseSchedule> other = repository.findByStudentIdOrderByWeekdayAscClassTimeAsc(OTHER_STUDENT);

        assertThat(other).hasSize(1);
        assertThat(other.get(0).getCourse()).isEqualTo("大学物理");
        assertThat(other)
                .extracting(CourseSchedule::getCourse)
                .doesNotContain("数据结构", "计算机网络", "高等数学", "数据库原理");
    }

    @Test
    @DisplayName("排序由查询完成：乱序写入后仍按星期、上课时间升序返回")
    void orderingIsDoneByTheQueryNotByInsertionOrder() {
        // 星期用序号存储，因此「按星期排序」在数据库里才有意义；
        // 若哪天有人把 weekday 改成「周一」字符串，本用例会立即失败。
        repository.save(new CourseSchedule(STUDENT, "数据库原理", "赵磊", 5, "08:00-09:40", "B305", 5, 16));
        repository.save(new CourseSchedule(STUDENT, "计算机网络", "王芳", 1, "14:00-15:40", "B201", 1, 16));
        repository.save(new CourseSchedule(STUDENT, "数据结构", "李明", 1, "08:00-09:40", "A302", 1, 16));
        repository.flush();

        assertThat(repository.findByStudentIdOrderByWeekdayAscClassTimeAsc(STUDENT))
                .extracting(CourseSchedule::getCourse)
                .containsExactly("数据结构", "计算机网络", "数据库原理");
    }

    @Test
    @DisplayName("自然唯一键生效：重复的 (学号, 课程, 星期, 时段) 被数据库拒绝")
    void naturalUniqueKeyIsEnforced() {
        // 该约束是「种子脚本可用 INSERT IGNORE 写成幂等」的前提，
        // 因此这里直接验证它确实存在于表结构里。
        seed();

        assertThatThrownBy(() -> jdbcTemplate.update(
                "INSERT INTO course_schedule "
                        + "(student_id, course, teacher, weekday, class_time, room, week_start, week_end) "
                        + "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                STUDENT, "数据结构", "李明", 1, "08:00-09:40", "A302", 1, 16
        )).isInstanceOf(DuplicateKeyException.class);
    }

    @Test
    @DisplayName("同一天多门课是常态：unique key 只约束同一时段，不约束同一天")
    void sameWeekdayAllowsMultipleCourses() {
        // 反向守卫：若把唯一键误设成 (student_id, weekday)，
        // 「同一天两门课」这种最常见的课表形态就会被数据库拒绝。
        seed();

        List<CourseSchedule> monday = repository
                .findByStudentIdOrderByWeekdayAscClassTimeAsc(STUDENT)
                .stream()
                .filter(course -> course.getWeekday() == 1)
                .toList();

        assertThat(monday).extracting(CourseSchedule::getCourse).containsExactly("数据结构", "计算机网络");
    }

    private List<CourseSchedule> weekCourses(int week) {
        return repository
                .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                        STUDENT, week, week);
    }
}
