package edu.ylu.campus.service;

import edu.ylu.campus.dto.ScoreResponse;
import edu.ylu.campus.entity.Score;
import edu.ylu.campus.repository.ScoreRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 成绩业务查询。
 *
 * <p>业务查询（含实体 -> DTO 的转换、学期过滤的分派与事务边界）收敛在 Service 层：
 * Controller 不直接操作 Repository，Repository 也不承载业务逻辑。
 *
 * <p>「不传学期就取全部、传了只取该学期」是业务语义，由本层决定；
 * Repository 只提供两种取数方式。
 */
@Service
public class ScoreService {

    private final ScoreRepository repository;

    public ScoreService(ScoreRepository repository) {
        this.repository = repository;
    }

    /**
     * 查询指定学生的成绩。
     *
     * @param studentId 学号
     * @param semester  学期，形如 {@code 2025-2026-1}；为 {@code null} 或空白时
     *                  不按学期过滤，返回该学生全部成绩
     * @return 按学期从新到旧、同学期按课程名升序排列的成绩；查无数据时返回空列表。
     *         空列表是**正常结果**（该学生该学期没出分），不是错误，
     *         因此这里不抛异常、Controller 也照常返回 200。
     */
    @Transactional(readOnly = true)
    public List<ScoreResponse> findScoresByStudent(String studentId, String semester) {
        // 空白学期按「未提供」处理，而不是拿空串去精确匹配：
        // ?semester= 这种空值是调用方的笔误（Python 侧不传时会直接省略该参数），
        // 若按字面匹配，返回的空列表会被误读成「这学期没有成绩」。
        boolean scopedToSemester = semester != null && !semester.isBlank();
        List<Score> rows = scopedToSemester
                ? repository.findByStudentIdAndSemesterOrderByCourseAsc(studentId, semester)
                : repository.findByStudentIdOrderBySemesterDescCourseAsc(studentId);
        return rows.stream()
                .map(ScoreResponse::from)
                .toList();
    }
}
