package edu.ylu.campus.controller;

import edu.ylu.campus.dto.ExamScheduleResponse;
import edu.ylu.campus.service.ExamService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 考试安排查询接口。
 *
 * <pre>
 * GET /api/exams?studentId=student-001
 * </pre>
 *
 * <p>Controller 只做协议层的事：绑定查询参数、委派 Service、返回响应体。
 * 不写查询逻辑、不接触 Repository、不处理实体。
 *
 * <p>{@code studentId} 是必填参数：Spring MVC 在参数缺失时自动返回 400，
 * 无需为此引入额外的校验组件。
 */
@RestController
@RequestMapping("/api/exams")
public class ExamController {

    private final ExamService examService;

    public ExamController(ExamService examService) {
        this.examService = examService;
    }

    /**
     * 查询指定学生的考试安排。
     *
     * @param studentId 学号（必填）
     * @return JSON 数组；查无数据时返回空数组 {@code []}
     */
    @GetMapping
    public List<ExamScheduleResponse> listExams(@RequestParam("studentId") String studentId) {
        return examService.findExamsByStudent(studentId);
    }
}
