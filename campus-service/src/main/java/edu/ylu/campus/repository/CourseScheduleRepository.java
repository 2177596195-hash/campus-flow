package edu.ylu.campus.repository;

import edu.ylu.campus.entity.CourseSchedule;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

/**
 * 课程安排仓储：只声明数据访问意图，不承载业务逻辑。
 *
 * <p>与 {@link ExamScheduleRepository} 一样用 Spring Data 派生查询，
 * 方法名即查询语义，因此本模块内没有手写 SQL。
 *
 * <p>排序放在查询里做（星期 -> 上课时间），理由同考试安排：课表按时间先后呈现
 * 是业务预期，不应交给调用方补排序。星期用序号参与排序，中文标签排序无意义，
 * 这一点见 {@link CourseSchedule} 的类注释。
 */
public interface CourseScheduleRepository extends JpaRepository<CourseSchedule, Long> {

    /**
     * 按学号查询整学期课表，按星期、上课时间升序返回。
     *
     * @param studentId 学号；无匹配数据时返回空列表（不抛异常）
     */
    List<CourseSchedule> findByStudentIdOrderByWeekdayAscClassTimeAsc(String studentId);

    /**
     * 按学号 + 教学周查询课表：只返回「覆盖该周」的课程。
     *
     * <p>「覆盖该周」即 {@code weekStart <= week <= weekEnd}。派生查询没有
     * {@code BETWEEN} 关键字，因此拆成两个条件；两个形参都传同一个周次，
     * 分别对应 {@code week_start} 的上界与 {@code week_end} 的下界。
     * 参数刻意不合并成「区间」写法，是为了让条件与字段一一对上、便于核对。
     *
     * @param studentId    学号
     * @param weekAtStart  {@code week_start} 的上界（即查询周次）
     * @param weekAtEnd    {@code week_end} 的下界（即查询周次）
     */
    List<CourseSchedule> findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
            String studentId, int weekAtStart, int weekAtEnd);
}
