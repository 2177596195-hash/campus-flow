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

import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * 成绩实体，对应 {@code score} 表。
 *
 * <p>与考试安排（{@link ExamSchedule}）、课程安排（{@link CourseSchedule}）保持同一套约定：
 * 字段最小、访问器显式实现、自然唯一键让种子脚本可用 {@code INSERT IGNORE} 写成幂等的。
 *
 * <p><b>为什么分数与学分用 {@link BigDecimal} 而不是 {@code double}</b>
 * <br>成绩是「精确的十进制数」：二进制浮点会出现 {@code 91.5} 存成
 * {@code 91.49999999999999} 这类表示误差，既会让「同一个分数是否相等」的
 * 判断出现意外，也会在使用它做加权计算时把误差放大。数据库侧相应地用
 * {@code DECIMAL(5,2)} / {@code DECIMAL(3,1)}，Java 侧用 {@link BigDecimal}，
 * 整条链路都不经过二进制浮点。
 *
 * <p><b>自然唯一键 {@code (student_id, semester, course)}</b>
 * <br>表达「同一学生在同一学期、同一门课只有一条成绩」这条业务约束。
 * 它也解释了为什么学期必须是成绩的一部分：同一门课可能在不同学期重修，
 * 那是有两条记录的两件事，不是同一条记录被覆盖。
 *
 * <p><b>没有 {@code exam_type} 字段</b>
 * <br>当前需求（查询某学生/某学期的成绩）不需要区分考试类型；按「不要凭空增加
 * 无意义字段」的原则不加。将来若出现「平时分 / 期末分分别查询」的真实需求，
 * 再加列并同步唯一键。
 */
@Entity
@Table(
        name = "score",
        uniqueConstraints = @UniqueConstraint(
                name = "uk_score_student_semester_course",
                columnNames = {"student_id", "semester", "course"}
        ),
        indexes = {
                @Index(name = "idx_score_student", columnList = "student_id"),
                @Index(name = "idx_score_student_semester", columnList = "student_id, semester")
        }
)
public class Score {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** 学号，当前阶段由调用方以固定测试学生传入（尚无统一身份认证）。 */
    @Column(name = "student_id", nullable = false, length = 64)
    private String studentId;

    /** 学期，形如 {@code 2025-2026-1}。 */
    @Column(name = "semester", nullable = false, length = 32)
    private String semester;

    /** 课程名称。 */
    @Column(name = "course", nullable = false, length = 128)
    private String course;

    /** 成绩分数，取值 0–100。 */
    @Column(name = "score", nullable = false, precision = 5, scale = 2)
    private BigDecimal score;

    /** 学分。 */
    @Column(name = "credit", nullable = false, precision = 3, scale = 1)
    private BigDecimal credit;

    /** 记录创建时间，由 {@link #onCreate()} 自动填充。 */
    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    /** JPA 规范要求的无参构造器，业务代码请使用带参构造器。 */
    protected Score() {
    }

    public Score(
            String studentId,
            String semester,
            String course,
            BigDecimal score,
            BigDecimal credit
    ) {
        this.studentId = studentId;
        this.semester = semester;
        this.course = course;
        this.score = score;
        this.credit = credit;
    }

    /**
     * 便捷构造器：接受 {@code double}，内部转成 {@link BigDecimal}。
     *
     * <p>只为让测试与种子装配读起来更直白（{@code 92.0} 比
     * {@code new BigDecimal("92.00")} 更接近业务表达）；转换走
     * {@link BigDecimal#valueOf(double)}（按 {@code Double.toString} 的十进制
     * 表示转换），因此不会引入二进制浮点误差后再落到库里。
     */
    public Score(String studentId, String semester, String course, double score, double credit) {
        this(studentId, semester, course, BigDecimal.valueOf(score), BigDecimal.valueOf(credit));
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

    public String getSemester() {
        return semester;
    }

    public String getCourse() {
        return course;
    }

    public BigDecimal getScore() {
        return score;
    }

    public BigDecimal getCredit() {
        return credit;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
