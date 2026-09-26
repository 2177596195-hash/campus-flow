"""校园业务服务（Spring Boot REST API）客户端。

设计要点
--------

1. **错误即文案**：``CampusApiError`` 家族的 ``str(exc)`` 是可直接转述给用户的
   中文描述。原因很直接——工具异常会被 ``handle_tool_error`` 原样写进
   ``ToolMessage`` 交给模型，因此异常消息本身就是产品文案，必须人话化，
   不能是 traceback。完整堆栈只进服务端日志（各抛出点均带 ``exc_info=True``）。

2. **失败分类而非笼统失败**：连接失败 / 超时 / 4xx / 5xx / 数据格式 分别对应
   一个异常类型。分类不只为可读性——它让「该重试」与「该改参数」可以区分，
   也为后续加退避重试、熔断留出接入点。

3. **可注入 HTTP 客户端**：``CampusApiClient(..., client=...)`` 允许注入任意
   ``httpx.Client``。测试据此挂上 ``httpx.MockTransport``，从而在**不启动
   Spring Boot、不联网**的前提下，走完真实的 httpx 代码路径（含超时与状态码），
   而不是把 HTTP 层整个 mock 掉。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Final
from urllib.parse import urlparse

import httpx

from app.config.settings import (
    CampusApiSettings,
    ConfigurationError,
    get_campus_api_settings,
)

logger = logging.getLogger(__name__)

#: 考试安排查询路径（相对服务基址）
EXAM_SCHEDULE_PATH: Final[str] = "/api/exams"

#: 课程安排查询路径（相对服务基址）
COURSE_SCHEDULE_PATH: Final[str] = "/api/courses"

#: 成绩查询路径（相对服务基址）
SCORE_PATH: Final[str] = "/api/scores"

#: 请假申请提交路径（相对服务基址）
LEAVE_PATH: Final[str] = "/api/leaves"

#: 两类请求在「被 4xx 拒绝」时的可读文案标签。
#:
#: 为什么必须区分：查询失败时用户该「确认查询条件」，提交失败时该「检查提交内容」——
#: 一旦混用，模型会照着错误消息给出错误的下一步建议。文案前缀同时用于日志。
_QUERY_LABEL: Final[str] = "查询请求"
_SUBMIT_LABEL: Final[str] = "提交请求"

#: 两类请求对应的纠正提示（与上面的标签配对使用）。
_QUERY_HINT: Final[str] = "请确认查询条件后重试。"
_SUBMIT_HINT: Final[str] = "请检查提交内容后重试。"

#: 未显式配置时的默认超时（秒）。取 5s：本服务与本机 Agent 同机部署，
#: 正常响应在毫秒级；超过 5s 更可能是服务已挂，早失败比久等更有价值。
DEFAULT_TIMEOUT: Final[float] = 5.0

#: 视为「本机」的主机名。这些地址的连接不经过环境代理，理由见
#: ``_direct_mounts`` 的说明。
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})


class CampusApiError(RuntimeError):
    """校园业务服务调用失败的统一基类。

    消息面向用户可读；调用方（``handle_tool_error``）会把它转成
    ``ToolMessage`` 交回模型，由模型用自然语言向用户解释。
    """


class CampusServiceUnavailable(CampusApiError):
    """服务不可达：连接被拒、DNS 失败或请求超时。"""


class CampusServiceRequestError(CampusApiError):
    """请求被服务端拒绝（HTTP 4xx），通常是查询参数问题。"""


class CampusServiceServerError(CampusApiError):
    """服务端内部错误（HTTP 5xx）。"""


class CampusServiceDataError(CampusApiError):
    """响应不是合法 JSON，或结构与约定不符。"""


def _direct_mounts(base_url: str) -> dict[str, httpx.HTTPTransport]:
    """目标为本机时，返回「直连、不读环境代理」的 transport 挂载项。

    为什么必须处理：``httpx`` 默认读取 ``HTTP_PROXY`` / ``HTTPS_PROXY``
    （以及 Windows 注册表里的系统代理），并且**不会**像 ``requests`` 那样
    自动绕过回环地址。实测后果很直观——本机 8081 上的 Spring Boot 停掉之后，
    请求被代理接走并返回 ``502 Bad Gateway``，于是「服务不可达」被误判成
    「服务端错误」，异常分类与给用户的建议都跟着失真。

    归因到根上：业务服务地址是**显式配置**的，Agent 需要知道「服务到底通不通」，
    这个问题不该由代理代为回答。因此回环地址一律直连；非回环地址（例如将来
    服务部署到别的机器）仍然遵循环境代理设置，企业内网代理不会被破坏。
    """

    host = (urlparse(base_url).hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return {}
    # IPv6 字面量在 URL 里必须带方括号，否则挂载键不是合法 URL 前缀
    host_key = f"[{host}]" if ":" in host else host
    return {
        f"{scheme}://{host_key}": httpx.HTTPTransport(trust_env=False)
        for scheme in ("http", "https")
    }


class CampusApiClient:
    """校园业务服务的只读 HTTP 客户端。

    Args:
        base_url: 服务基址，例如 ``http://localhost:8080``。
        timeout: 单次请求超时（秒）。
        default_student_id: 未显式传学号时使用的当前学生标识。
        client: 注入的 ``httpx.Client``；``None`` 时自建。
            测试用它挂 ``httpx.MockTransport``。

            注意：本类**不依赖注入客户端的 base_url**，所有请求都拼成绝对 URL。
            早期实现把基址交给 ``httpx.Client(base_url=...)`` 并传相对路径，
            结果一旦注入的客户端没设 base_url，httpx 就会拿到相对 URL
            （cookie 处理阶段直接抛 ``ValueError: unknown url type``）。
            基址只保留 ``self._base_url`` 这一个来源，行为对两种客户端都一致。
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        default_student_id: str = "",
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._default_student_id = default_student_id
        self._owns_client = client is None
        # 自建客户端对回环地址直连（理由见 _direct_mounts）；注入的客户端
        # 由调用方决定代理策略，本类不改动它。
        self._client = client if client is not None else httpx.Client(
            timeout=timeout, mounts=_direct_mounts(self._base_url)
        )

    # ------------------------------------------------------------------ 公共属性

    @property
    def base_url(self) -> str:
        """服务基址（已去掉尾部斜杠）。"""

        return self._base_url

    @property
    def timeout(self) -> float:
        """单次请求超时（秒）。"""

        return self._timeout

    # ------------------------------------------------------------------ 业务方法

    def fetch_exam_schedule(self, student_id: str | None = None) -> list[dict[str, Any]]:
        """查询指定学生的考试安排。

        本方法只保证「拿到一个 JSON 数组，且每个元素是对象」——这是接口的
        传输层契约。字段级的业务校验与结构整理属于工具层职责，
        本层不做，避免业务规则下沉到基础设施。

        Args:
            student_id: 学号；``None`` 时使用构造时配置的当前学生。

        Returns:
            服务返回的考试记录列表（原始字段，未做字段裁剪）。

        Raises:
            CampusServiceUnavailable: 服务不可达或超时。
            CampusServiceRequestError: HTTP 4xx。
            CampusServiceServerError: HTTP 5xx。
            CampusServiceDataError: 响应不是合法 JSON，或不是数组 / 元素不是对象。
        """

        target = student_id or self._default_student_id
        payload = self._get_json(EXAM_SCHEDULE_PATH, params={"studentId": target})
        return self._expect_records(payload)

    def fetch_course_schedule(
        self,
        student_id: str | None = None,
        week: int | None = None,
    ) -> list[dict[str, Any]]:
        """查询指定学生的课程安排（课表）。

        与 ``fetch_exam_schedule`` 同构：只保证传输层契约（数组 + 元素为对象），
        字段归一化留给工具层，业务规则不下沉到基础设施。

        ``week`` 的过滤在**服务端**完成（Spring Boot 的 ``/api/courses`` 按
        ``week_start <= week <= week_end`` 过滤），本方法只负责把条件透传成
        query 参数。把这层规则留在服务端有两个理由：一是「第 3 周有哪些课」
        需要数据库做范围比较并走索引，二是周次区间属于教务业务规则，
        在客户端再抄一份，两份迟早会漂移。

        Args:
            student_id: 学号；``None`` 时使用构造时配置的当前学生。
            week: 教学周序号；``None`` 时不限周次，返回整学期课表。

        Returns:
            服务返回的课程记录列表（原始字段，未做字段裁剪）。

        Raises:
            CampusServiceUnavailable: 服务不可达或超时。
            CampusServiceRequestError: HTTP 4xx。
            CampusServiceServerError: HTTP 5xx。
            CampusServiceDataError: 响应不是合法 JSON，或不是数组 / 元素不是对象。
        """

        target = student_id or self._default_student_id
        params: dict[str, str] = {"studentId": target}
        if week is not None:
            # query 参数一律是字符串；显式转换而不是依赖 httpx 的隐式行为，
            # 让「实际发出的请求长什么样」在代码里直接可读。
            params["week"] = str(week)
        payload = self._get_json(COURSE_SCHEDULE_PATH, params=params)
        return self._expect_records(payload)

    def fetch_scores(
        self,
        student_id: str | None = None,
        semester: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询指定学生的成绩。

        与 ``fetch_exam_schedule`` / ``fetch_course_schedule`` 同构：只保证传输层
        契约（数组 + 元素为对象），字段归一化留给工具层，业务规则不下沉到基础设施。

        ``semester`` 的过滤在**服务端**完成（Spring Boot 的 ``/api/scores`` 按
        ``semester = ?`` 精确匹配），本方法只负责把条件透传成 query 参数。
        把这层筛选留在服务端有两点理由：一是学期是数据库里的一列、该由数据库来筛；
        二是「哪些学期有成绩」属于业务事实，在客户端再判断一次就是第二份事实来源。

        Args:
            student_id: 学号；``None`` 时使用构造时配置的当前学生。
            semester: 学期，形如 ``2025-2026-1``；``None`` 时不限学期，返回该学生
                全部成绩（省略该 query 参数，而不是发一个空值）。

        Returns:
            服务返回的成绩记录列表（原始字段，未做字段裁剪）。

        Raises:
            CampusServiceUnavailable: 服务不可达或超时。
            CampusServiceRequestError: HTTP 4xx。
            CampusServiceServerError: HTTP 5xx。
            CampusServiceDataError: 响应不是合法 JSON，或不是数组 / 元素不是对象。
        """

        target = student_id or self._default_student_id
        params: dict[str, str] = {"studentId": target}
        if semester is not None:
            params["semester"] = semester
        payload = self._get_json(SCORE_PATH, params=params)
        return self._expect_records(payload)

    def submit_leave_application(
        self,
        *,
        start_time: str,
        end_time: str,
        reason: str,
        student_id: str | None = None,
    ) -> dict[str, Any]:
        """提交一条学生请假申请。

        这是本客户端的**第一个写操作**：它只负责把请求发给业务服务并保证
        「拿回一个 JSON 对象」——申请编号、状态、时间归属等业务语义都由
        Spring Boot 决定，客户端不预判、不重试、不推断。

        ``student_id`` 是关键字参数且排在最后，是刻意的：他属于**身份**而不是
        用户意图，调用方（MCP 适配层）从配置里补齐，模型永远看不到它。

        请求体字段名与服务端 DTO 一致（``studentId`` / ``startTime`` /
        ``endTime`` / ``reason``）；时间必须是 ISO 8601 文本，这一层不做格式
        兜底——格式归一化是 ``app.tools.leave_contract`` 的职责，放在那里才能让
        两条链路（MCP 与将来的直连工具）表现一致。

        Args:
            start_time: 请假开始时间，形如 ``2026-09-28T08:00``。
            end_time: 请假结束时间，形如 ``2026-09-30T18:00``。
            reason: 请假事由。
            student_id: 学号；``None`` 时使用构造时配置的当前学生。

        Returns:
            服务返回的申请记录对象（原始字段，未做字段裁剪）。

        Raises:
            CampusServiceUnavailable: 服务不可达或超时。
            CampusServiceRequestError: HTTP 4xx（提交内容被拒绝）。
            CampusServiceServerError: HTTP 5xx。
            CampusServiceDataError: 响应不是合法 JSON，或不是对象。
        """

        target = student_id or self._default_student_id
        payload = self._post_json(
            LEAVE_PATH,
            json_body={
                "studentId": target,
                "startTime": start_time,
                "endTime": end_time,
                "reason": reason,
            },
        )
        return self._expect_record(payload)

    def _expect_record(self, payload: Any) -> dict[str, Any]:
        """校验「响应是一个 JSON 对象」并原样返回。

        与 ``_expect_records`` 成对：查询接口返回数组，提交接口返回单条对象。
        单条场景下把「不是对象」变成明确失败，避免把 ``None`` 或裸字符串
        当成业务记录继续往下传（下游会以一个看不懂的 ``AttributeError`` 暴露）。
        """

        if not isinstance(payload, dict):
            logger.warning(
                "校园业务服务返回了非对象结构: %s (%s)",
                type(payload).__name__,
                self._base_url,
            )
            raise CampusServiceDataError(
                "校园业务服务返回的数据格式异常（不是对象），请联系管理员。"
            )
        return payload

    def _expect_records(self, payload: Any) -> list[dict[str, Any]]:
        """校验「响应是一个对象数组」并原样返回。

        抽出来是因为考试与课程两个接口的传输层契约完全一致：同一套校验写两遍，
        必然出现「改了一处、忘了另一处」——而这类漂移不会报错，
        只会让其中一侧静默地接受畸形数据。
        """

        if not isinstance(payload, list):
            logger.warning(
                "校园业务服务返回了非数组结构: %s (%s)", type(payload).__name__, self._base_url
            )
            raise CampusServiceDataError("校园业务服务返回的数据格式异常，请联系管理员。")

        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                logger.warning("校园业务服务返回的第 %d 条记录不是对象", index)
                raise CampusServiceDataError(
                    f"校园业务服务返回的数据格式异常（第 {index} 条不是对象），请联系管理员。"
                )

        return list(payload)

    # ------------------------------------------------------------------ 传输层

    def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        """发起 GET 并把响应解析为 JSON（查询类请求的统一入口）。"""

        return self._request_json(
            path,
            method="GET",
            params=params,
            label=_QUERY_LABEL,
            hint=_QUERY_HINT,
        )

    def _post_json(self, path: str, *, json_body: dict[str, Any]) -> Any:
        """发起 POST 并把响应解析为 JSON（提交类请求的统一入口）。

        与查询的差别只有两处，且都是刻意的：

        1. 4xx 时**带上服务端给出的具体原因**（``{"message": ...}``）。提交被拒
           几乎总是调用方传了不合法的内容，把服务端的原因原样交给模型，它才能
           知道该改什么；查询的 4xx 主要是参数缺失，服务端没有额外信息可给。
        2. 文案改成「检查提交内容」而不是「确认查询条件」。
        """

        return self._request_json(
            path,
            method="POST",
            json_body=json_body,
            label=_SUBMIT_LABEL,
            hint=_SUBMIT_HINT,
            include_server_detail=True,
        )

    def _request_json(
        self,
        path: str,
        *,
        method: str,
        label: str,
        hint: str,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        include_server_detail: bool = False,
    ) -> Any:
        """发起请求并把响应解析为 JSON，同时把各类失败翻译成可读异常。

        请求使用**绝对 URL**（``self._base_url + path``），不依赖底层客户端的
        base_url 设置，理由见类文档字符串。

        查询与提交共用这一处，是因为失败分类（超时 / 不可达 / 4xx / 5xx / JSON
        畸形）对两者完全一致；分成两份写迟早会出现「改了一处、忘了另一处」——
        而这类漂移不报错，只会让某一条链路上的失败悄悄变成另一类。
        """

        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(method, url, params=params, json=json_body)
        except httpx.TimeoutException as exc:
            logger.warning("校园业务服务响应超时: %s%s", self._base_url, path, exc_info=True)
            raise CampusServiceUnavailable("校园业务服务响应超时，请稍后再试。") from exc
        except httpx.HTTPError as exc:
            # 连接被拒 / 地址解析失败等传输层错误：对外统一表述为「暂时不可用」，
            # 不把 URL 与底层错误细节暴露给模型。
            logger.warning("校园业务服务不可达: %s%s", self._base_url, path, exc_info=True)
            raise CampusServiceUnavailable("校园业务服务暂时不可用，请稍后再试。") from exc

        status = response.status_code
        if 400 <= status < 500:
            logger.warning(
                "校园业务服务拒绝%s: HTTP %s %s%s", label, status, self._base_url, path
            )
            detail = self._server_detail(response) if include_server_detail else ""
            raise CampusServiceRequestError(
                f"校园业务{label}被拒绝（HTTP {status}）{detail}，{hint}"
            )
        if status >= 500:
            logger.warning("校园业务服务内部错误: HTTP %s %s%s", status, self._base_url, path)
            raise CampusServiceServerError(
                f"校园业务服务内部错误（HTTP {status}），请稍后再试。"
            )

        try:
            return response.json()
        except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
            logger.warning("校园业务服务响应不是合法 JSON: %s%s", self._base_url, path, exc_info=True)
            raise CampusServiceDataError(
                "校园业务服务返回的数据格式异常，请联系管理员。"
            ) from exc

    @staticmethod
    def _server_detail(response: httpx.Response) -> str:
        """从错误响应体里取出服务端给的可读原因；取不到时返回空串。

        空串是**正常**结果而不是异常：Spring Boot 对「参数缺失」这类由框架自己
        判定的 400 不一定带 ``message``，此时退回通用文案即可，不该因此再抛一个错。
        """

        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, Mapping):
            return ""
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return ""
        return f"：{message.strip()}"

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        """关闭底层连接池（仅关闭由本对象自建的客户端）。"""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CampusApiClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@lru_cache(maxsize=1)
def get_campus_api_client() -> CampusApiClient:
    """返回进程内共享的校园业务服务客户端。

    首次调用读取配置并建立连接池；与 ``get_settings()`` 一样属于
    「首次使用才校验」的惰性策略，因此导入本模块不产生副作用。

    Raises:
        ConfigurationError: 地址 / 超时 / 学生标识配置非法。
    """

    settings: CampusApiSettings = get_campus_api_settings()
    logger.info(
        "校园业务服务客户端已装配: base_url=%s timeout=%ss student_id=%s",
        settings.base_url,
        settings.timeout,
        settings.default_student_id,
    )
    return CampusApiClient(
        settings.base_url,
        timeout=settings.timeout,
        default_student_id=settings.default_student_id,
    )


def reset_campus_api_client() -> None:
    """关闭并丢弃缓存的客户端，使下次调用按最新配置重新装配。

    用途：改过 ``CAMPUS_SERVICE_BASE_URL`` 等环境变量后需要重新读取配置时，
    以及跨进程操作后需要重开连接池时。
    """

    if get_campus_api_client.cache_info().currsize:
        get_campus_api_client().close()
    get_campus_api_client.cache_clear()


__all__ = [
    "COURSE_SCHEDULE_PATH",
    "DEFAULT_TIMEOUT",
    "EXAM_SCHEDULE_PATH",
    "LEAVE_PATH",
    "SCORE_PATH",
    "CampusApiClient",
    "CampusApiError",
    "CampusServiceDataError",
    "CampusServiceRequestError",
    "CampusServiceServerError",
    "CampusServiceUnavailable",
    "get_campus_api_client",
    "reset_campus_api_client",
]
