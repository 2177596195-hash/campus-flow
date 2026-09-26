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

import java.time.LocalDateTime;

/**
 * 课程安排实体，对应 {@code course_schedule} 表。
 *
 * <p>与考试安排（{@link ExamSchedule}）保持同一套约定：字段最小、访问器显式实现、
 * 自然唯一键让种子脚本可用 {@code INSERT IGNORE} 写成幂等的。
 *
 * <p><b>为什么 {@code weekday} 存序号而不是「周一」字符串</b>
 * <br>课表要按星期先后呈现，而中文「周一 / 周二 / …」按字符排序得到的顺序是
 * 一、三、二、五、六、四、日——完全错乱。存 1..7 的序号既可被数据库正确排序，
 * 也让「星期几」这件事只有一种表示；对外展示的「周一」由 DTO 渲染
 * （见 {@code CourseScheduleResponse}）。
 *
 * <p><b>为什么周次拆成 {@code week_start} / {@code week_end}</b>
 * <br>「第 3 周有什么课」是一条必须在数据库里完成过滤的查询。若把周次存成
 * {@code "1-16周"} 这样的展示字符串，过滤就只能靠字符串解析，既写不出可用的
 * 索引，也会把展示格式耦合进查询条件。因此库里存区间上下界，
 * 展示字符串（{@code 1-16周}）由 DTO 统一渲染，与 Python 侧原有 mock 的
 * 输出格式完全一致。
 *
 * <p>自然唯一键 {@code (student_id, course, weekday, class_time)} 表达
 * 「同一学生的同一门课在同一时间只排一次」；同一门课排在不同星期（如数据结构
 * 周一、周四各一次）仍是两行，这是真实课表的常态。
 */
@Entity
@Table(
        name = "course_schedule",
        uniqueConstraints = @UniqueConstraint(
                name = "uk_course_schedule_slot",
                columnNames = {"student_id", "course", "weekday", "class_time"}
        ),
        indexes = @Index(
                name = "idx_course_schedule_student",
                columnList = "student_id, weekday, class_time"
        )
)
public class CourseSchedule {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** 学号，当前阶段由调用方以固定测试学生传入（尚无统一身份认证）。 */
    @Column(name = "student_id", nullable = false, length = 64)
    private String studentId;

    /** 课程名称。 */
    @Column(name = "course", nullable = false, length = 128)
    private String course;

    /** 任课教师。 */
    @Column(name = "teacher", nullable = false, length = 64)
    private String teacher;

    /** 星期序号，1 = 周一 … 7 = 周日。 */
    @Column(name = "weekday", nullable = false)
    private int weekday;

    /** 上课时段，形如 {@code 08:00-09:40}。 */
    @Column(name = "class_time", nullable = false, length = 32)
    private String classTime;

    /** 上课教室。 */
    @Column(name = "room", nullable = false, length = 64)
    private String room;

    /** 起始教学周（含）。 */
    @Column(name = "week_start", nullable = false)
    private int weekStart;

    /** 结束教学周（含）。 */
    @Column(name = "week_end", nullable = false)
    private int weekEnd;

    /** 记录创建时间，由 {@link #onCreate()} 自动填充。 */
    @Column(name = "created_at", nullable = false, updatable = false)
    private LocalDateTime createdAt;

    /** JPA 规范要求的无参构造器，业务代码请使用带参构造器。 */
    protected CourseSchedule() {
    }

    public CourseSchedule(
            String studentId,
            String course,
            String teacher,
            int weekday,
            String classTime,
            String room,
            int weekStart,
            int weekEnd
    ) {
        this.studentId = studentId;
        this.course = course;
        this.teacher = teacher;
        this.weekday = weekday;
        this.classTime = classTime;
        this.room = room;
        this.weekStart = weekStart;
        this.weekEnd = weekEnd;
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

    public String getTeacher() {
        return teacher;
    }

    public int getWeekday() {
        return weekday;
    }

    public String getClassTime() {
        return classTime;
    }

    public String getRoom() {
        return room;
    }

    public int getWeekStart() {
        return weekStart;
    }

    public int getWeekEnd() {
        return weekEnd;
    }

    public LocalDateTime getCreatedAt() {
        return createdAt;
    }
}
