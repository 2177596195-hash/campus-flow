package edu.ylu.campus.controller;

import edu.ylu.campus.entity.CourseSchedule;
import edu.ylu.campus.repository.CourseScheduleRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 课程接口层测试：按真实 HTTP 语义验证 REST 契约。
 *
 * <p>走完整链路（Controller -> Service -> Repository -> MySQL），但用
 * {@code MockMvc} 而不启动真实端口。这正是 Python 侧 Tool 依赖的那份契约：
 * 字段名、字段格式、可选 week 参数与空结果行为。
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class CourseControllerTest {

    private static final String PATH = "/api/courses";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private CourseScheduleRepository repository;

    private void seed() {
        repository.saveAll(List.of(
                new CourseSchedule("student-001", "数据结构", "李明", 1, "08:00-09:40", "A302", 1, 16),
                new CourseSchedule("student-001", "计算机网络", "王芳", 1, "14:00-15:40", "B201", 1, 16),
                new CourseSchedule("student-001", "数据库原理", "赵磊", 5, "08:00-09:40", "B305", 5, 16),
                new CourseSchedule("student-002", "大学物理", "周敏", 2, "10:00-11:40", "F203", 1, 16)
        ));
        repository.flush();
    }

    @Test
    @DisplayName("GET /api/courses?studentId=... 返回按星期、上课时间升序的 JSON 数组")
    void listsCoursesOfTheStudent() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(content().contentTypeCompatibleWith("application/json"))
                .andExpect(jsonPath("$.length()").value(3))
                .andExpect(jsonPath("$[0].course").value("数据结构"))
                .andExpect(jsonPath("$[0].teacher").value("李明"))
                .andExpect(jsonPath("$[0].weekday").value("周一"))
                .andExpect(jsonPath("$[0].time").value("08:00-09:40"))
                .andExpect(jsonPath("$[0].room").value("A302"))
                .andExpect(jsonPath("$[0].weeks").value("1-16周"))
                .andExpect(jsonPath("$[1].course").value("计算机网络"))
                .andExpect(jsonPath("$[2].course").value("数据库原理"));
    }

    @Test
    @DisplayName("不传 week 时返回该学生全部课程")
    void omittingWeekReturnsWholeTimetable() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(3));
    }

    @Test
    @DisplayName("传入 week 时只返回覆盖该周的课程")
    void filtersByWeekWhenProvided() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001").param("week", "3"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(2))
                .andExpect(jsonPath("$[0].course").value("数据结构"))
                .andExpect(jsonPath("$[1].course").value("计算机网络"))
                // 第 5 周才开课的课程不出现在第 3 周
                .andExpect(jsonPath("$[?(@.course == '数据库原理')]").isEmpty());
    }

    @Test
    @DisplayName("studentId 与 week 组合过滤生效，且学生数据不串")
    void combinesStudentAndWeek() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-002").param("week", "3"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(1))
                .andExpect(jsonPath("$[0].course").value("大学物理"));
    }

    @Test
    @DisplayName("响应体只含 Agent 需要的六个字段，不泄露实体内部字段")
    void responseDoesNotLeakEntityFields() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].id").doesNotExist())
                .andExpect(jsonPath("$[0].studentId").doesNotExist())
                .andExpect(jsonPath("$[0].weekStart").doesNotExist())
                .andExpect(jsonPath("$[0].weekEnd").doesNotExist())
                .andExpect(jsonPath("$[0].classTime").doesNotExist())
                .andExpect(jsonPath("$[0].createdAt").doesNotExist());
    }

    @Test
    @DisplayName("查不到课程时返回 200 + 空数组（而不是 404）")
    void returnsEmptyArrayForUnknownStudent() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-999"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    @DisplayName("该周无课时也返回 200 + 空数组")
    void returnsEmptyArrayForWeekWithoutCourses() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001").param("week", "17"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    @DisplayName("缺少 studentId 参数时返回 400")
    void rejectsRequestWithoutStudentId() throws Exception {
        mockMvc.perform(get(PATH))
                .andExpect(status().isBadRequest());
    }

    @Test
    @DisplayName("week 不是数字时返回 400（框架类型转换失败，不落到 Service）")
    void rejectsNonNumericWeek() throws Exception {
        mockMvc.perform(get(PATH).param("studentId", "student-001").param("week", "第3周"))
                .andExpect(status().isBadRequest());
    }
}
