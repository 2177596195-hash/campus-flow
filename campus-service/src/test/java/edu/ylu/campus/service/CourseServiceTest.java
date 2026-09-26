package edu.ylu.campus.service;

import edu.ylu.campus.dto.CourseScheduleResponse;
import edu.ylu.campus.entity.CourseSchedule;
import edu.ylu.campus.repository.CourseScheduleRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 课程服务层测试：验证业务查询语义（含周次分派）与实体 -> DTO 的转换。
 *
 * <p>刻意断言**输出格式**（星期为「周一」、周次为「1-16周」）而不只是字段个数，
 * 因为这两个渲染结果正是 Python 侧 Tool 依赖的对外契约：这次改造把课程数据
 * 从 Python 本地搬到了 MySQL，但 Agent 看到的字段格式必须一字不差，
 * 否则回答措辞会莫名其妙地变形。
 */
@SpringBootTest
@ActiveProfiles("test")
@Transactional
class CourseServiceTest {

    private static final String STUDENT = "student-001";

    @Autowired
    private CourseService courseService;

    @Autowired
    private CourseScheduleRepository repository;

    private void seed() {
        repository.saveAll(List.of(
                new CourseSchedule(STUDENT, "数据结构", "李明", 1, "08:00-09:40", "A302", 1, 16),
                new CourseSchedule(STUDENT, "计算机网络", "王芳", 1, "14:00-15:40", "B201", 1, 16),
                new CourseSchedule(STUDENT, "数据库原理", "赵磊", 5, "08:00-09:40", "B305", 5, 16),
                new CourseSchedule("student-002", "大学物理", "周敏", 2, "10:00-11:40", "F203", 1, 16)
        ));
        repository.flush();
    }

    @Test
    @DisplayName("返回 DTO 列表：星期与周次已渲染成稳定中文格式，时段原样透出")
    void returnsDtosWithStableRendering() {
        seed();

        List<CourseScheduleResponse> courses = courseService.findCoursesByStudent(STUDENT, null);

        assertThat(courses).hasSize(3);
        CourseScheduleResponse first = courses.get(0);
        assertThat(first.course()).isEqualTo("数据结构");
        assertThat(first.teacher()).isEqualTo("李明");
        assertThat(first.weekday()).isEqualTo("周一");   // 由序号 1 渲染
        assertThat(first.time()).isEqualTo("08:00-09:40");
        assertThat(first.room()).isEqualTo("A302");
        assertThat(first.weeks()).isEqualTo("1-16周");   // 由 (1, 16) 渲染
    }

    @Test
    @DisplayName("周次区间不同则渲染不同：高等数学 1-18 周")
    void rendersDifferentWeekRanges() {
        repository.save(new CourseSchedule(STUDENT, "高等数学", "张伟", 2, "08:00-09:40", "C105", 1, 18));
        repository.flush();

        assertThat(courseService.findCoursesByStudent(STUDENT, null))
                .extracting(CourseScheduleResponse::weeks)
                .contains("1-18周");
    }

    @Test
    @DisplayName("week 为 null 时返回整学期课表；给出 week 时只返回覆盖该周的课程")
    void weekIsOptionalAndDispatchesToTheRightQuery() {
        seed();

        List<CourseScheduleResponse> whole = courseService.findCoursesByStudent(STUDENT, null);
        List<CourseScheduleResponse> week3 = courseService.findCoursesByStudent(STUDENT, 3);
        List<CourseScheduleResponse> week8 = courseService.findCoursesByStudent(STUDENT, 8);

        assertThat(whole).hasSize(3);
        assertThat(week3).extracting(CourseScheduleResponse::course).doesNotContain("数据库原理");
        assertThat(week3).hasSize(2);
        assertThat(week8).extracting(CourseScheduleResponse::course).contains("数据库原理");
        assertThat(week8).hasSize(3);
    }

    @Test
    @DisplayName("按周查询仍保持服务端排序：星期 -> 上课时间")
    void weekQueryKeepsServerSideOrdering() {
        seed();

        assertThat(courseService.findCoursesByStudent(STUDENT, 8))
                .extracting(CourseScheduleResponse::course)
                .containsExactly("数据结构", "计算机网络", "数据库原理");
    }

    @Test
    @DisplayName("未知学号返回空列表（这周没课不是错误）")
    void returnsEmptyListForUnknownStudent() {
        seed();

        assertThat(courseService.findCoursesByStudent("student-999", null)).isEmpty();
        assertThat(courseService.findCoursesByStudent("student-999", 3)).isEmpty();
    }

    @Test
    @DisplayName("学生之间数据隔离：查 student-002 不会带出 student-001 的课")
    void isolatesStudents() {
        seed();

        assertThat(courseService.findCoursesByStudent("student-002", null))
                .extracting(CourseScheduleResponse::course)
                .containsExactly("大学物理");
    }

    @Test
    @DisplayName("对外结构被 DTO 收敛：不暴露 id / studentId / weekStart / createdAt 等内部字段")
    void dtoExposesOnlyAgentFacingFields() {
        seed();

        CourseScheduleResponse response = courseService.findCoursesByStudent(STUDENT, null).get(0);

        assertThat(CourseScheduleResponse.class.getRecordComponents())
                .extracting(component -> component.getName())
                .containsExactly("course", "teacher", "weekday", "time", "room", "weeks");
        assertThat(response.toString()).doesNotContain("student-001");
    }

    @Test
    @DisplayName("库里出现非法星期序号时大声失败，而不是伪装成周一")
    void invalidWeekdayFailsLoudly() {
        // weekday 是枚举语义（1..7），越界说明数据写错了。
        // 静默回退会让一条错误数据看起来像正常课表，比直接失败更难排查。
        repository.save(new CourseSchedule(STUDENT, "测试课程", "某某", 9, "08:00-09:40", "Z999", 1, 16));
        repository.flush();

        assertThatThrownBy(() -> courseService.findCoursesByStudent(STUDENT, null))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("星期序号");
    }
}
