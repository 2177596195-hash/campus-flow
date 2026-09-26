package edu.ylu.campus.repository;

import edu.ylu.campus.entity.Score;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

/**
 * 成绩仓储：只声明数据访问意图，不承载业务逻辑。
 *
 * <p>与 {@link ExamScheduleRepository} / {@link CourseScheduleRepository} 一样，
 * 用 Spring Data 派生查询，方法名即查询语义，因此本模块内没有手写 SQL。
 *
 * <p><b>排序放在查询里做</b>：成绩列表按「学期从新到旧、同学期按课程名」呈现，
 * 这是业务预期（先看当前学期），不应交给调用方补排序。学期是形如
 * {@code 2025-2026-1} 的定长格式，字典序与时间序一致，因此可以直接
 * {@code ORDER BY semester DESC}，不需要额外解析。
 */
public interface ScoreRepository extends JpaRepository<Score, Long> {

    /**
     * 按学号查询全部成绩，按学期从新到旧、同学期按课程名升序返回。
     *
     * @param studentId 学号；无匹配数据时返回空列表（不抛异常）
     */
    List<Score> findByStudentIdOrderBySemesterDescCourseAsc(String studentId);

    /**
     * 按学号 + 学期查询成绩；学期内按课程名升序。
     *
     * <p>学期相等是精确匹配（{@code semester = ?}）：{@code 2025-2026-1} 与
     * {@code 2024-2025-2} 是两个不同的学期，不做前缀或模糊匹配。
     *
     * @param studentId 学号
     * @param semester  学期，形如 {@code 2025-2026-1}
     */
    List<Score> findByStudentIdAndSemesterOrderByCourseAsc(String studentId, String semester);
}
