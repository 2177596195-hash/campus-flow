package edu.ylu.campus.controller;

import edu.ylu.campus.dto.LeaveApplicationRequest;
import edu.ylu.campus.dto.LeaveApplicationResponse;
import edu.ylu.campus.service.InvalidLeaveApplicationException;
import edu.ylu.campus.service.LeaveApplicationService;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * 请假申请提交接口。
 *
 * <pre>
 * POST /api/leaves
 * { "studentId": "student-001",
 *   "startTime": "2026-10-12 08:00",
 *   "endTime":   "2026-10-14 18:00",
 *   "reason":    "感冒发烧需就医" }
 * -&gt; 201 Created
 * { "id": 1, "studentId": "student-001", "startTime": "2026-10-12T08:00:00",
 *   "endTime": "2026-10-14T18:00:00", "reason": "感冒发烧需就医",
 *   "status": "PENDING", "createdAt": "2026-10-12T08:03:11" }
 * </pre>
 *
 * <p>Controller 只做协议层的事：绑定请求体、委派 Service、把失败翻译成状态码。
 * 不写校验规则、不接触 Repository、不处理实体。
 *
 * <p><b>成功返回 201 而不是 200</b>
 * <br>这是本服务第一个「创建资源」的接口，201 是它的正确语义：请求不仅被成功
 * 处理，还产生了一个新的、可被引用的资源（申请编号 {@code id}）。沿用 200 会让
 * 调用方无法区分「查询成功」与「创建成功」——Python 侧虽然两者都当作成功，
 * 但语义准确的状态码是后续加幂等键、重试策略时唯一能依赖的信号。
 *
 * <p><b>为什么异常处理写在本类里，而不是全局 {@code @ControllerAdvice}</b>
 * <br>{@link InvalidLeaveApplicationException} 是请假提交独有的失败语义，把它的
 * 映射留在产生它的控制器内，既不会影响已有的查询接口，也避免了为一个控制器
 * 引入全局异常处理这一额外的装配面。请求体缺失 / 不是合法 JSON 由 Spring MVC
 * 自身判成 400，同样无需本类介入。
 */
@RestController
@RequestMapping("/api/leaves")
public class LeaveApplicationController {

    private final LeaveApplicationService leaveApplicationService;

    public LeaveApplicationController(LeaveApplicationService leaveApplicationService) {
        this.leaveApplicationService = leaveApplicationService;
    }

    /**
     * 提交一条请假申请。
     *
     * @param request 请求体，含 {@code studentId} / {@code startTime} /
     *                {@code endTime} / {@code reason}
     * @return 新建的申请记录（含申请编号与初始状态 {@code PENDING}）
     */
    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public LeaveApplicationResponse submit(@RequestBody LeaveApplicationRequest request) {
        return leaveApplicationService.submit(request);
    }

    /**
     * 把「入参不合法」翻译成 400 + 一句可读原因。
     *
     * <p>响应体形如 {@code {"message": "..."}}：这是 Python 侧
     * {@code CampusApiClient._server_detail} 主动读取的字段，取出后会拼进交给模型的
     * 错误文案里，让「哪一项不合法」能到达用户。没有这个处理器的话，未捕获的
     * 运行时异常会变成 500，把「你传错了」误报成「服务故障」。
     *
     * @param exception Service 抛出的校验失败
     * @return 含可读原因的响应体
     */
    @ExceptionHandler(InvalidLeaveApplicationException.class)
    @ResponseStatus(HttpStatus.BAD_REQUEST)
    public Map<String, String> handleInvalidRequest(InvalidLeaveApplicationException exception) {
        return Map.of("message", exception.getMessage());
    }
}
