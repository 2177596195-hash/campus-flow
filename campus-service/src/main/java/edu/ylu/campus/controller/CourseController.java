package edu.ylu.campus.controller;

import edu.ylu.campus.dto.CourseScheduleResponse;
import edu.ylu.campus.service.CourseService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 课程安排查询接口。
 *
 * <pre>
 * GET /api/courses?studentId=student-001
 * GET /api/courses?studentId=student-001&amp;week=3
 * </pre>
 *
 * <p>Controller 只做协议层的事：绑定查询参数、委派 Service、返回响应体。
 * 不写查询逻辑、不接触 Repository、不处理实体。
 *
 * <p>{@code studentId} 是必填参数：Spring MVC 在参数缺失时自动返回 400。
 * {@code week} 是可选参数（{@code required = false}），不传即返回整学期课表；
 * 非数字的 {@code week} 由 Spring 的类型转换失败处理，同样返回 400——
 * 这些都是框架的既有行为，不需要额外校验组件。
 */
@RestController
@RequestMapping("/api/courses")
public class CourseController {

    private final CourseService courseService;

    public CourseController(CourseService courseService) {
        this.courseService = courseService;
    }

    /**
     * 查询指定学生的课程安排。
     *
     * @param studentId 学号（必填）
     * @param week      教学周序号（可选）；不传时返回该学生全部课程
     * @return JSON 数组；查无数据时返回空数组 {@code []}
     */
    @GetMapping
    public List<CourseScheduleResponse> listCourses(
            @RequestParam("studentId") String studentId,
            @RequestParam(value = "week", required = false) Integer week
    ) {
        return courseService.findCoursesByStudent(studentId, week);
    }
}
