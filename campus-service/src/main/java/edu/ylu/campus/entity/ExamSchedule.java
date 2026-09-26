package edu.ylu.campus.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Index;
import jakarta.persistence.PrePersist;
import jakarta.persistence.Table;
import jakarta.persistence.UniqueConstraint;

import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.LocalTime;

/**
 * 考试安排实体，对应 {@code exam_schedule} 表。
 *
 * <p>字段刻意保持最小：只放查询考试安排真正需要的列，不做「企业级」扩展。
 *
 * <p>自然唯一键 {@code (student_id, course, exam_date)} 表达一条业务约束：
 * 同一学生的同一门课在同一天只会有一场考试。它同时让种子数据脚本可以用
 * {@code INSERT IGNORE} 写成幂等的，重复启动服务不会插入重复行。
 *
 * <p>访问器为显式实现（未使用 Lombok）：本模块是仓库里唯一的 Java 工程，
 * 没有既有的注解处理器约定，显式写法让构建链更少依赖、评审更直观。
 */
@Entity
@Table(
        name = "exam_schedule",
        uniqueConstraints = @UniqueConstraint(
                name = "uk_exam_schedule_student_course_date",
                columnNames = {"student_id", "course", "exam_date"}
        ),
        indexes = @Index(
                name = "idx_exam_schedule_student_date",
                columnList = "student_id, exam_date"
        )
)
public class ExamSchedule {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** 学号，当前阶段由调用方以固定测试学生传入（尚无统一身份认证）。 */
    @Column(name = "student_id", nullable = false, length = 64)
    private String studentId;

    /** 考试科目名称。 */
    @Column(name = "course", nullable = false, length = 128)
    private String course;

    /** 考试日期（仅日期部分）。 */
    @Column(name = "exam_date", nullable = false)
    private LocalDate examDate;

    /** 开考时间（仅时间部分）。 */
    @Column(name = "exam_time", nullable = false)
    private LocalTime examTime;

    /** 考场。 */
    @Column(name = "room", nullable = false, length = 64)
    private String room;

    /** 记录创建时间，由 {@link #onCreate()} 自动填充。 */
    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    /** JPA 规范要求的无参构造器，业务代码请使用带参构造器。 */
    protected ExamSchedule() {
    }

    public ExamSchedule(
            String studentId,
            String course,
            LocalDate examDate,
            LocalTime examTime,
            String room
    ) {
        this.studentId = studentId;
        this.course = course;
        this.examDate = examDate;
        this.examTime = examTime;
        this.room = room;
    }

    @PrePersist
    void onCreate() {
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

    public String getCourse() {
        return course;
    }

    public LocalDate getExamDate() {
        return examDate;
    }

    public LocalTime getExamTime() {
        return examTime;
    }

    public String getRoom() {
        return room;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
