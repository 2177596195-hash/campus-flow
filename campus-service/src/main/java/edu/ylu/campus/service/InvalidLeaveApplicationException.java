package edu.ylu.campus.service;

/**
 * 请假申请入参不合法（缺失、空白、时间格式无法识别或时间倒置）。
 *
 * <p><b>为什么需要一个专门的异常，而不是返回 {@code null} 或抛 {@code IllegalArgumentException}</b>
 * <br>这个失败是**可预期的业务拒绝**，不是程序缺陷，因此它必须：
 *
 * <ol>
 *   <li>被明确地映射成 HTTP 400（而不是 500）——「你提交的内容不对」与
 *       「服务坏了」对调用方是完全不同的两件事，Python 侧据此决定是「改参数重试」
 *       还是「稍后再试」；</li>
 *   <li>携带一句**可直接展示的中文原因**——与 Python 侧
 *       「异常消息即产品文案」的做法对齐，服务端起因经 REST 原样回到模型，
 *       用户才能知道该补哪一项。</li>
 * </ol>
 *
 * <p>继承 {@link RuntimeException} 而非受检异常：参数校验失败不该迫使每一层调用方
 * 在签名上声明它，图省事的调用方十有八九会把它包成 500。
 */
public class InvalidLeaveApplicationException extends RuntimeException {

    public InvalidLeaveApplicationException(String message) {
        super(message);
    }
}
