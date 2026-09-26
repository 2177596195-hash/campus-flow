"""校园业务服务（Spring Boot）HTTP 客户端。

本模块只负责「如何跟校园业务服务说话」：

- 服务基址来自配置（``CAMPUS_SERVICE_BASE_URL``），代码内不散落 URL；
- 负责 HTTP 请求、超时、状态码分类与 JSON 解析；
- 把各种失败统一翻译成**面向模型可读的中文异常**（见 ``CampusApiError`` 家族）。

本模块**不做**的事：不写 SQL、不连接数据库、不做业务规则判断，
也不缓存业务数据（缓存策略属于后续阶段的话题）。

对外主要入口::

    from app.infrastructure.campus_api import get_campus_api_client

    client = get_campus_api_client()
    rows = client.fetch_exam_schedule()
    courses = client.fetch_course_schedule(week=3)
    scores = client.fetch_scores(semester="2025-2026-1")
    leave = client.submit_leave_application(
        start_time="2026-09-28T08:00", end_time="2026-09-30T18:00", reason="感冒发烧"
    )
"""

from app.infrastructure.campus_api.client import (
    COURSE_SCHEDULE_PATH,
    EXAM_SCHEDULE_PATH,
    LEAVE_PATH,
    SCORE_PATH,
    CampusApiClient,
    CampusApiError,
    CampusServiceDataError,
    CampusServiceRequestError,
    CampusServiceServerError,
    CampusServiceUnavailable,
    get_campus_api_client,
    reset_campus_api_client,
)

__all__ = [
    "COURSE_SCHEDULE_PATH",
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
