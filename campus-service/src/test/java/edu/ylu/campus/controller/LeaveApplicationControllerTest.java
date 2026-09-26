package edu.ylu.campus.controller;

import edu.ylu.campus.repository.LeaveApplicationRepository;
import org.hamcrest.Matchers;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 请假接口层测试：按真实 HTTP 语义验证 REST 契约。
 *
 * <p>走完整链路（Controller -&gt; Service -&gt; Repository -&gt; MySQL），但用
 * {@code MockMvc} 而不启动真实端口。这正是 Python 侧
 * {@code CampusApiClient.submit_leave_application} 依赖的那份契约：
 * 路径、请求体字段、成功码 201、失败码 400 与失败响应里的 {@code message}。
 *
 * <p>失败用例都断言 {@code $.message} 存在——那是 Python 侧
 * {@code _server_detail} 唯一会读取的字段，也是「哪一项不合法」能到达用户的通道。
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class LeaveApplicationControllerTest {

    private static final String PATH = "/api/leaves";
    private static final String STUDENT = "student-001";

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private LeaveApplicationRepository repository;

    private static String body(String studentId, String start, String end, String reason) {
        return """
                {"studentId": %s, "startTime": %s, "endTime": %s, "reason": %s}
                """.formatted(json(studentId), json(start), json(end), json(reason));
    }

    /** 极简 JSON 字符串转义：测试里出现的都是固定字面量，够用即可。 */
    private static String json(String value) {
        if (value == null) {
            return "null";
        }
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\"";
    }

    @Test
    @DisplayName("POST /api/leaves 成功返回 201 与完整申请记录")
    void createsApplicationAndReturns201() throws Exception {
        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(STUDENT, "2026-10-12 08:00", "2026-10-14 18:00", "感冒发烧需就医")))
                .andExpect(status().isCreated())
                .andExpect(content().contentTypeCompatibleWith(MediaType.APPLICATION_JSON))
                .andExpect(jsonPath("$.id").isNumber())
                .andExpect(jsonPath("$.studentId").value(STUDENT))
                // 时间是 ISO 8601 字符串（不是数字时间戳、不是数组），Python 侧按文本归一化
                .andExpect(jsonPath("$.startTime").value(Matchers.startsWith("2026-10-12T08:00")))
                .andExpect(jsonPath("$.endTime").value(Matchers.startsWith("2026-10-14T18:00")))
                .andExpect(jsonPath("$.reason").value("感冒发烧需就医"))
                .andExpect(jsonPath("$.status").value("PENDING"))
                .andExpect(jsonPath("$.createdAt").isString());

        // 契约的另一半：响应说创建成功，库里就真的多了一条
        assertThat(repository.findByStudentIdOrderByCreatedAtDesc(STUDENT)).hasSize(1);
    }

    @Test
    @DisplayName("缺少学生标识时返回 400，并给出可读原因")
    void rejectsMissingStudentId() throws Exception {
        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(null, "2026-10-12 08:00", "2026-10-12 18:00", "事由")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value(Matchers.containsString("studentId")));
    }

    @Test
    @DisplayName("缺少事由时返回 400，并给出可读原因")
    void rejectsMissingReason() throws Exception {
        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(STUDENT, "2026-10-12 08:00", "2026-10-12 18:00", "   ")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value(Matchers.containsString("事由")));
    }

    @Test
    @DisplayName("时间格式无法识别时返回 400，而不是 500")
    void rejectsUnparsableTimeWithBadRequest() throws Exception {
        // 「你传错了」必须是 4xx：Python 侧据此判定「该改参数重试」而非「服务故障」。
        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(STUDENT, "明天早上", "2026-10-12 18:00", "事由")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value(Matchers.containsString("格式无法识别")));
    }

    @Test
    @DisplayName("结束时间早于开始时间时返回 400")
    void rejectsReversedTimeRange() throws Exception {
        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(STUDENT, "2026-10-14 18:00", "2026-10-12 08:00", "时间倒置")))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.message").value(Matchers.containsString("不能早于")));
    }

    @Test
    @DisplayName("请求体缺失或不是合法 JSON 时返回 400")
    void rejectsMalformedRequestBody() throws Exception {
        mockMvc.perform(post(PATH).contentType(MediaType.APPLICATION_JSON))
                .andExpect(status().isBadRequest());

        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{不是 JSON"))
                .andExpect(status().isBadRequest());
    }

    @Test
    @DisplayName("失败时不写库：被拒的提交不留下任何记录")
    void failedSubmissionLeavesNoTrace() throws Exception {
        mockMvc.perform(post(PATH)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body(STUDENT, "不是时间", "2026-10-12 18:00", "事由")))
                .andExpect(status().isBadRequest());

        assertThat(repository.findByStudentIdOrderByCreatedAtDesc(STUDENT)).isEmpty();
    }
}
