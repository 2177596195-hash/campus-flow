-- CampusFlow 校园业务服务 —— 表结构（幂等）
--
-- 由 Spring Boot 的 spring.sql.init 在每次启动时执行；
-- 全部使用 IF NOT EXISTS / 幂等写法，因此重复启动安全。

CREATE TABLE IF NOT EXISTS exam_schedule (
    id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    student_id VARCHAR(64)  NOT NULL COMMENT '学号',
    course     VARCHAR(128) NOT NULL COMMENT '考试科目',
    exam_date  DATE         NOT NULL COMMENT '考试日期',
    exam_time  TIME         NOT NULL COMMENT '开考时间',
    room       VARCHAR(64)  NOT NULL COMMENT '考场',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    -- 自然唯一键：同一学生、同一门课、同一天只会有一场考试。
    -- 它同时让 data.sql 可以用 INSERT IGNORE 写成幂等种子脚本。
    UNIQUE KEY uk_exam_schedule_student_course_date (student_id, course, exam_date),
    -- 查询恒为「按学号取某人的考试，按时间排序」，因此建联合索引。
    KEY idx_exam_schedule_student_date (student_id, exam_date)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '考试安排';

CREATE TABLE IF NOT EXISTS course_schedule (
    id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    student_id VARCHAR(64)  NOT NULL COMMENT '学号',
    course     VARCHAR(128) NOT NULL COMMENT '课程名称',
    teacher    VARCHAR(64)  NOT NULL COMMENT '任课教师',
    -- 存 1..7 的序号而不是「周一」字符串：中文星期按字符排序是乱序
    -- （一、三、二、五、六、四、日），序号才能让数据库排对课表顺序。
    -- 对外展示的「周一」由 DTO 渲染。
    weekday    TINYINT      NOT NULL COMMENT '星期序号：1=周一 … 7=周日',
    -- 形如 08:00-09:40 的展示串。列名用 class_time 而不是 time：TIME 是 MySQL
    -- 的数据类型关键字，避开它可以免掉跨驱动的标识符引用差异。
    class_time VARCHAR(32)  NOT NULL COMMENT '上课时段，形如 08:00-09:40',
    room       VARCHAR(64)  NOT NULL COMMENT '上课教室',
    -- 周次存区间上下界而不是「1-16周」字符串：按周查询（第 3 周有什么课）
    -- 必须由数据库完成过滤，字符串无法参与索引与范围比较。
    -- 对外展示的「1-16周」由 DTO 渲染。
    week_start TINYINT      NOT NULL COMMENT '起始教学周（含）',
    week_end   TINYINT      NOT NULL COMMENT '结束教学周（含）',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    -- 自然唯一键：同一学生的同一门课在同一时间只排一次。
    -- 它同时让 data.sql 可以用 INSERT IGNORE 写成幂等种子脚本。
    -- 同一门课排在别的星期（如数据结构周一、周四各一次）仍是两行，属正常课表。
    UNIQUE KEY uk_course_schedule_slot (student_id, course, weekday, class_time),
    -- 查询恒为「按学号取某人的课表，按星期与上课时间排序」。
    KEY idx_course_schedule_student (student_id, weekday, class_time),
    -- 按周过滤走 (week_start, week_end) 的范围比较。
    KEY idx_course_schedule_weeks (week_start, week_end)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '课程安排';

CREATE TABLE IF NOT EXISTS score (
    id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
    student_id VARCHAR(64)  NOT NULL COMMENT '学号',
    semester   VARCHAR(32)  NOT NULL COMMENT '学期，形如 2025-2026-1',
    course     VARCHAR(128) NOT NULL COMMENT '课程名称',
    -- 用 DECIMAL 而不是 FLOAT / DOUBLE：成绩是「精确的十进制数」，
    -- 二进制浮点会出现 91.5 -> 91.499999... 这类表示误差，
    -- 直接比较与加权计算都会踩到。DECIMAL(5,2) 覆盖 0.00–999.99。
    score      DECIMAL(5,2) NOT NULL COMMENT '成绩分数（0-100）',
    -- 学分同样是十进制数（常见 0.5 的倍数），DECIMAL(3,1) 覆盖 0.0–99.9。
    credit     DECIMAL(3,1) NOT NULL COMMENT '学分',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    -- 自然唯一键：同一学生在同一学期、同一门课只有一条成绩。
    -- 它同时让 data.sql 可以用 INSERT IGNORE 写成幂等种子脚本。
    UNIQUE KEY uk_score_student_semester_course (student_id, semester, course),
    -- 查询恒为「按学号取某人成绩」，因此建学号索引。
    KEY idx_score_student (student_id),
    -- 「某学期的成绩」是主要过滤条件；联合索引让「学号 + 学期」一次命中。
    KEY idx_score_student_semester (student_id, semester)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '学生成绩';

CREATE TABLE IF NOT EXISTS leave_application (
    id         BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键（申请编号）',
    student_id VARCHAR(64)  NOT NULL COMMENT '学号',
    -- 起止时间用 DATETIME 而不是 DATE：请假可以精确到时刻（半天假、几小时假），
    -- 只存日期会把「上午请假下午回来」压成整天。
    start_time DATETIME     NOT NULL COMMENT '请假开始时间',
    end_time   DATETIME     NOT NULL COMMENT '请假结束时间',
    reason     VARCHAR(512) NOT NULL COMMENT '请假事由',
    -- 状态用字符串而不是数值枚举：待审核 / 已通过 / 已驳回，可读且便于后续
    -- 直接扩展取值，不必回头翻译 0/1/2。默认 PENDING 让「提交即待审核」
    -- 这条业务规则由数据库兜底，即便某条插入路径漏传状态也不会写出脏数据。
    status     VARCHAR(16)  NOT NULL DEFAULT 'PENDING' COMMENT '审核状态：PENDING/APPROVED/REJECTED',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (id),
    -- 查询恒为「按学号取某人的请假记录、按提交时间排序」，
    -- 因此建 (student_id, created_at) 联合索引；不建唯一键——
    -- 请假是追加型事务数据，同一学生可以为不同时段（甚至同一时段重提）留下多条申请，
    -- 每条都该有自己的申请编号，而不是被自然唯一键合并掉。
    KEY idx_leave_application_student_created (student_id, created_at)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_general_ci COMMENT = '学生请假申请';

