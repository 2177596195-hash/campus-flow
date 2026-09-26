package edu.ylu.campus.service;

import edu.ylu.campus.dto.ExamScheduleResponse;
import edu.ylu.campus.repository.ExamScheduleRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

/**
 * 考试安排业务查询。
 *
 * <p>业务查询（含实体 → DTO 的转换与事务边界）收敛在 Service 层：
 * Controller 不直接操作 Repository，Repository 也不承载业务逻辑。
 */
@Service
public class ExamService {

    private final ExamScheduleRepository repository;

    public ExamService(ExamScheduleRepository repository) {
        this.repository = repository;
    }

    /**
     * 查询指定学生的考试安排。
     *
     * @param studentId 学号
     * @return 按考试时间升序排列的考试安排；查无数据时返回空列表。
     *         空列表是**正常结果**（该学生目前没有考试），不是错误，
     *         因此这里不抛异常、Controller 也照常返回 200。
     */
    @Transactional(readOnly = true)
    public List<ExamScheduleResponse> findExamsByStudent(String studentId) {
        return repository.findByStudentIdOrderByExamDateAscExamTimeAsc(studentId)
                .stream()
                .map(ExamScheduleResponse::from)
                .toList();
    }
}
