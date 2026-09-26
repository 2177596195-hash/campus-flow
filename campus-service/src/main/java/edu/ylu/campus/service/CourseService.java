package edu.ylu.campus.service;

import edu.ylu.campus.dto.CourseScheduleResponse;
import edu.ylu.campus.entity.CourseSchedule;
import edu.ylu.campus.repository.CourseScheduleRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 课程安排业务查询。
 *
 * <p>业务查询（含实体 -> DTO 的转换、周次过滤的分派与事务边界）收敛在 Service 层：
 * Controller 不直接操作 Repository，Repository 也不承载业务逻辑。
 *
 * <p>「按周过滤」这件业务规则同样落在这里：Repository 只提供两种取数方式，
 * 「不传 week 就取全部、传了只取该周」是业务语义，由本层决定。
 */
@Service
public class CourseService {

    private final CourseScheduleRepository repository;

    public CourseService(CourseScheduleRepository repository) {
        this.repository = repository;
    }

    /**
     * 查询指定学生的课程安排。
     *
     * @param studentId 学号
     * @param week      教学周序号；为 {@code null} 时不限周次，返回整学期课表
     * @return 按星期、上课时间升序排列的课程安排；查无数据时返回空列表。
     *         空列表是**正常结果**（该学生这周没课），不是错误，
     *         因此这里不抛异常、Controller 也照常返回 200。
     */
    @Transactional(readOnly = true)
    public List<CourseScheduleResponse> findCoursesByStudent(String studentId, Integer week) {
        List<CourseSchedule> rows = (week == null)
                ? repository.findByStudentIdOrderByWeekdayAscClassTimeAsc(studentId)
                : repository
                        .findByStudentIdAndWeekStartLessThanEqualAndWeekEndGreaterThanEqualOrderByWeekdayAscClassTimeAsc(
                                studentId, week, week);
        return rows.stream()
                .map(CourseScheduleResponse::from)
                .toList();
    }
}
