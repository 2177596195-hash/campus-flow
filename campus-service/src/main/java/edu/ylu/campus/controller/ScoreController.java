package edu.ylu.campus.controller;

import edu.ylu.campus.dto.ScoreResponse;
import edu.ylu.campus.service.ScoreService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

/**
 * 成绩查询接口。
 *
 * <pre>
 * GET /api/scores?studentId=student-001
 * GET /api/scores?studentId=student-001&amp;semester=2025-2026-1
 * </pre>
 *
 * <p>Controller 只做协议层的事：绑定查询参数、委派 Service、返回响应体。
 * 不写查询逻辑、不接触 Repository、不处理实体。
 *
 * <p>{@code studentId} 是必填参数：Spring MVC 在参数缺失时自动返回 400。
 * {@code semester} 是可选参数（{@code required = false}），不传即返回该学生
 * 全部成绩；「空白视为未提供」的归一化在 Service 里做（属于业务语义）。
 */
@RestController
@RequestMapping("/api/scores")
public class ScoreController {

    private final ScoreService scoreService;

    public ScoreController(ScoreService scoreService) {
        this.scoreService = scoreService;
    }

    /**
     * 查询指定学生的成绩。
     *
     * @param studentId 学号（必填）
     * @param semester  学期（可选），形如 {@code 2025-2026-1}；不传时返回该学生全部成绩
     * @return JSON 数组；查无数据时返回空数组 {@code []}
     */
    @GetMapping
    public List<ScoreResponse> listScores(
            @RequestParam("studentId") String studentId,
            @RequestParam(value = "semester", required = false) String semester
    ) {
        return scoreService.findScoresByStudent(studentId, semester);
    }
}
