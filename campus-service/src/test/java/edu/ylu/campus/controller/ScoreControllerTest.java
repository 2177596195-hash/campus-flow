package edu.ylu.campus.controller;

import edu.ylu.campus.entity.Score;
import edu.ylu.campus.repository.ScoreRepository;
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
 * 成绩接口层测试：按真实 HTTP 语义验证 REST 契约。
 *
 * <p>走完整链路（Controller -> Service -> Repository -> MySQL），但用
 * {@code MockMvc} 而不启动真实端口。这正是 Python 侧 Tool 依赖的那份契约：
 * 字段名、字段格式、可选 semester 参数与空结果行为。
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class ScoreControllerTest {

    private static final String PATH = "/api/scores";
    private static final String CURRENT_SEMESTER = "2025-2026-1";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ScoreRepository repository;

    private void seed() {
        repository.saveAll(List.of(
                new Score("student-001", CURRENT_SEMESTER, "数据结构", 92.0, 4.0),
                new Score("student-001", CURRENT_SEMESTER, "计算机网络", 88.0, 3.0),
                new Score("student-001", "2024-2025-2", "高等数学", 84.0, 5.0),
                new Score("student-002", CURRENT_SEMESTER, "大学物理", 90.0, 4.0)
        ));
        repository.flush();
    }

    @Test
    @DisplayName("GET /api/scores?studentId=... 返回按学期从新到旧的 JSON 数组")
    void listsScoresOfTheStudent() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(content().contentTypeCompatibleWith("application/json"))
                .andExpect(jsonPath("$.length()").value(3))
                .andExpect(jsonPath("$[0].semester").value(CURRENT_SEMESTER))
                .andExpect(jsonPath("$[0].course").value("数据结构"))
                .andExpect(jsonPath("$[0].score").value(92.0))
                .andExpect(jsonPath("$[0].credit").value(4.0))
                .andExpect(jsonPath("$[2].semester").value("2024-2025-2"));
    }

    @Test
    @DisplayName("成绩与学分是 JSON 数字，不是字符串")
    void scoreAndCreditAreJsonNumbers() throws Exception {
        // 数值类型是可被依赖的契约：模型要直接比较大小、做加权，
        // 若被字符串化成 "92.0" 就得自己转换，且排序会退化成字典序。
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].score").isNumber())
                .andExpect(jsonPath("$[0].credit").isNumber());
    }

    @Test
    @DisplayName("不传 semester 时返回该学生全部成绩")
    void omittingSemesterReturnsAllScores() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(3));
    }

    @Test
    @DisplayName("传入 semester 时只返回该学期成绩")
    void filtersBySemesterWhenProvided() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001").param("semester", CURRENT_SEMESTER))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(2))
                .andExpect(jsonPath("$[?(@.course == '高等数学')]").isEmpty());

        mockMvc.perform(get(PATH).param("studentId", "student-001").param("semester", "2024-2025-2"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(1))
                .andExpect(jsonPath("$[0].course").value("高等数学"));
    }

    @Test
    @DisplayName("studentId 与 semester 组合过滤生效，且学生数据不串")
    void combinesStudentAndSemester() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-002").param("semester", CURRENT_SEMESTER))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(1))
                .andExpect(jsonPath("$[0].course").value("大学物理"));
    }

    @Test
    @DisplayName("响应体只含 Agent 需要的四个字段，不泄露实体内部字段")
    void responseDoesNotLeakEntityFields() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].id").doesNotExist())
                .andExpect(jsonPath("$[0].studentId").doesNotExist())
                .andExpect(jsonPath("$[0].student_id").doesNotExist())
                .andExpect(jsonPath("$[0].createdAt").doesNotExist())
                .andExpect(jsonPath("$[0].created_at").doesNotExist());
    }

    @Test
    @DisplayName("查不到成绩时返回 200 + 空数组（而不是 404）")
    void returnsEmptyArrayForUnknownStudent() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-999"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    @DisplayName("该学期没出分时也返回 200 + 空数组")
    void returnsEmptyArrayForSemesterWithoutScores() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001").param("semester", "2023-2024-1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.length()").value(0));
    }

    @Test
    @DisplayName("缺少 studentId 参数时返回 400")
    void rejectsRequestWithoutStudentId() throws Exception {
        mockMvc.perform(get(PATH))
                .andExpect(status().isBadRequest());
    }
}
