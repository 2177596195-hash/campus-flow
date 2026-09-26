package edu.ylu.campus.repository;

import edu.ylu.campus.entity.ExamSchedule;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

/**
 * 考试安排仓储：只声明数据访问意图，不承载业务逻辑。
 *
 * <p>方法名即查询语义（Spring Data 派生查询），因此本模块内没有手写 SQL。
 * 排序放在查询里做：考试安排按时间先后呈现是业务预期，不应交给调用方补排序。
 */
public interface ExamScheduleRepository extends JpaRepository<ExamSchedule, Long> {

    /**
     * 按学号查询考试安排，按考试日期、开考时间升序返回。
     *
     * @param studentId 学号；无匹配数据时返回空列表（不抛异常）
     */
    List<ExamSchedule> findByStudentIdOrderByExamDateAscExamTimeAsc(String studentId);
}
