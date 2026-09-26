package edu.ylu.campus.controller;

import edu.ylu.campus.entity.ExamSchedule;
import edu.ylu.campus.repository.ExamScheduleRepository;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDate;
import java.time.LocalTime;
import java.util.List;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 接口层测试：按真实 HTTP 语义验证 REST 契约。
 *
 * <p>走完整链路（Controller → Service → Repository → MySQL），
 * 但用 {@code MockMvc} 而不启动真实端口，因此不占用端口、不受本机端口冲突影响。
 * 这正是 Python 侧 Tool 依赖的那份契约：字段名、类型与空结果行为。
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class ExamControllerTest {

    private static final String PATH = "/api/exams";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ExamScheduleRepository repository;

    private void seed() {
        repository.saveAll(List.of(
                new ExamSchedule("student-001", "计算机网络", LocalDate.of(2026, 9, 28), LocalTime.of(14, 0), "A302"),
                new ExamSchedule("student-001", "数据结构", LocalDate.of(2026, 9, 30), LocalTime.of(19, 0), "B201")
        ));
        repository.flush();
    }

    @Test
    @DisplayName("GET /api/exams?studentId=... 返回按时间升序的 JSON 数组")
    void listsExamsOfTheStudent() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(content().contentTypeCompatibleWith("application/json"))
                .andExpect(jsonPath("$.length()").value(2))
                .andExpect(jsonPath("$[0].course").value("计算机网络"))
                .andExpect(jsonPath("$[0].date").value("2026-09-28"))
                .andExpect(jsonPath("$[0].time").value("14:00"))
                .andExpect(jsonPath("$[0].room").value("A302"))
                .andExpect(jsonPath("$[1].course").value("数据结构"))
                .andExpect(jsonPath("$[1].room").value("B201"));
    }

    @Test
    @DisplayName("响应体只含 Agent 需要的四个字段，不泄露实体内部字段")
    void responseDoesNotLeakEntityFields() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-001"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].id").doesNotExist())
                .andExpect(jsonPath("$[0].studentId").doesNotExist())
                .andExpect(jsonPath("$[0].createdAt").doesNotExist())
                .andExpect(jsonPath("$[0].examDate").doesNotExist());
    }

    @Test
    @DisplayName("查不到考试时返回 200 + 空数组（而不是 404）")
    void returnsEmptyArrayForUnknownStudent() throws Exception {
        seed();

        mockMvc.perform(get(PATH).param("studentId", "student-999"))
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
