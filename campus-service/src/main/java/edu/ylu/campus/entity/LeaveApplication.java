package edu.ylu.campus.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.PrePersist;
import jakarta.persistence.Table;

import java.time.LocalDateTime;

/**
 * 请假申请实体，对应 {@code leave_application} 表。
 *
 * <p>与考试安排（{@link ExamSchedule}）、课程安排（{@link CourseSchedule}）、
 * 成绩（{@link Score}）保持同一套约定：字段最小、访问器显式实现、
 * {@link GenerationType#IDENTITY} 主键、{@link PrePersist} 填自动字段。
 *
 * <p><b>与前三个实体最关键的区别：这是「追加型事务数据」</b>
 * <br>考试成绩、课程安排是「关于某个学生的事实」——同一学生同一学期同一门课
 * 只该有一条，因此都有自然唯一键，种子脚本靠 {@code INSERT IGNORE} 保持幂等。
 * 请假申请则是**一次写入即产生一条新记录**的流水：同一学生可以为不同时段请假，
 * 甚至为同一时段重复提交（那是两条申请、两个申请编号），因此这里**刻意不设自然
 * 唯一键**，{@code data.sql} 也不为它造种子——申请只应该由真实的提交动作产生。
 *
 * <p><b>时间用 {@link LocalDateTime} 而不是 {@code LocalDate}</b>
 * <br>请假可以精确到时刻（半天假、几小时的外出假），只存日期会把
 * 「上午请假、下午回来」压成整天。数据库侧相应是 {@code DATETIME}。
 *
 * <p><b>初始状态由 {@link #onCreate()} 兜底，不由调用方传</b>
 * <br>「提交即待审核」是这条链路的业务规则。让它落在实体的生命周期回调里，
 * 意味着任何一条插入路径（本类构造器、将来的批量导入）都不可能写出没有状态的
 * 记录；数据库侧还有 {@code DEFAULT 'PENDING'} 作为第二道防线。
 */
@Entity
@Table(
        name = "leave_application",
        indexes = @Index(
                name = "idx_leave_application_student_created",
                columnList = "student_id, created_at"
        )
)
public class LeaveApplication {

    /** 提交后的初始状态：待审核。与 Python 侧 {@code leave_contract.STATUS_PENDING} 同值。 */
    public static final String STATUS_PENDING = "PENDING";

    /** 学号列的最大长度（与 {@code schema.sql} 的 {@code VARCHAR(64)} 对齐）。 */
    public static final int STUDENT_ID_MAX_LENGTH = 64;

    /** 事由列的最大长度（与 {@code schema.sql} 的 {@code VARCHAR(512)} 对齐）。 */
    public static final int REASON_MAX_LENGTH = 512;

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** 学号，当前阶段由调用方以固定测试学生传入（尚无统一身份认证）。 */
    @Column(name = "student_id", nullable = false, length = STUDENT_ID_MAX_LENGTH)
    private String studentId;

    /** 请假开始时间。 */
    @Column(name = "start_time", nullable = false)
    private LocalDateTime startTime;

    /** 请假结束时间。 */
    @Column(name = "end_time", nullable = false)
    private LocalDateTime endTime;

    /** 请假事由。 */
    @Column(name = "reason", nullable = false, length = REASON_MAX_LENGTH)
    private String reason;

    /** 审核状态，形如 {@code PENDING} / {@code APPROVED} / {@code REJECTED}。 */
    @Column(name = "status", nullable = false, length = 16)
    private String status;

    /** 提交时间，由 {@link #onCreate()} 自动填充。 */
    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    /** JPA 规范要求的无参构造器，业务代码请使用带参构造器。 */
    protected LeaveApplication() {
    }

    public LeaveApplication(
            String studentId,
            LocalDateTime startTime,
            LocalDateTime endTime,
            String reason
    ) {
        this.studentId = studentId;
        this.startTime = startTime;
        this.endTime = endTime;
        this.reason = reason;
    }

    @PrePersist
    void onCreate() {
        // 状态与提交时间都在此处兜底：调用方无需（也无从）指定它们，
        // 因此「谁提交、何时提交」不可能被伪造或被漏填。
        if (status == null) {
            status = STATUS_PENDING;
        }
        if (createdAt == null) {
            createdAt = LocalDateTime.now();
        }
    }

    public Long getId() {
        return id;
    }

    public String getStudentId() {
        return studentId;
    }

    public LocalDateTime getStartTime() {
        return startTime;
    }

    public LocalDateTime getEndTime() {
        return endTime;
    }

    public String getReason() {
        return reason;
    }

    public String getStatus() {
        return status;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
