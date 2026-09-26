package edu.ylu.campus.repository;

import edu.ylu.campus.entity.LeaveApplication;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

/**
 * 请假申请仓储：只声明数据访问意图，不承载业务逻辑。
 *
 * <p>与 {@link ScoreRepository} 等一致，用 Spring Data 派生查询，方法名即查询语义，
 * 因此本模块内没有手写 SQL。
 *
 * <p><b>本阶段只提供「写入」与「按学号读取」两件事</b>
 * <br>{@link JpaRepository} 自带的 {@code save} 已经足够支撑提交链路；
 * 这里额外声明一个按学号排序的派生查询，是因为 {@code schema.sql} 上的
 * {@code idx_leave_application_student_created} 索引正是为它而建——
 * 索引必须有与之对应的访问意图，否则就是凭空的索引。本阶段的查询能力只用于
 * **验证提交结果确实落库**（测试与联调），以及为下一阶段的「我的请假记录」查询
 * 预留同一个取数入口；对外**不暴露**查询接口。
 *
 * <p>排序在查询里完成、且是「从新到旧」：请假记录天然按提交时间倒序呈现
 * （最近一条在最前），这个顺序属于业务预期，不交给调用方补排序。
 */
public interface LeaveApplicationRepository extends JpaRepository<LeaveApplication, Long> {

    /**
     * 按学号查询请假申请，按提交时间从新到旧返回。
     *
     * @param studentId 学号；无匹配数据时返回空列表（不抛异常）
     */
    List<LeaveApplication> findByStudentIdOrderByCreatedAtDesc(String studentId);
}
