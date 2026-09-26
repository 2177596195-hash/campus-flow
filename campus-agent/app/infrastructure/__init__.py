"""基础设施层：与外部系统交互的技术细节。

本层存在的意义是把「怎么和外部系统说话」从业务代码里彻底剥离：

- ``campus_api``：校园业务服务（Spring Boot REST API）的 HTTP 客户端。

分层约定::

    app.tools（Agent 能力）
        |
        v
    app.infrastructure.campus_api（HTTP / 超时 / 状态码 / JSON）
        |
        v
    Spring Boot -> MySQL

Agent 工具只依赖本层提供的客户端对象，**不出现** URL 拼接、状态码判断、
JSON 解析等细节；同理，本层不出现「考试」「课表」之外的业务规则，
也不接触向量库、Embedding 与 RAG 清单（由测试守卫强制）。
"""
