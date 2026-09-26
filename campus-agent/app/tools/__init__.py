"""工具包：Agent 可调用的原子能力。

当前提供三项**只读查询**工具，全部经 ``app.infrastructure.campus_api``
调用校园业务服务（Spring Boot），由该服务访问 MySQL：

- ``get_exam_schedule``：``GET /api/exams``（当 ``CAMPUS_MCP_ENABLED=true`` 时
  改由 Campus MCP Server 暴露，见 ``app.mcp.tools``）；
- ``get_course_schedule``：``GET /api/courses``；
- ``get_score``：``GET /api/scores``。

注意：这里只负责导出工具对象；「哪些工具暴露给模型」由
``app/agent/nodes/tool_agent.py`` 的 ``AGENT_TOOLS`` 单点决定，
避免出现两份互相漂移的工具清单。
"""

from app.tools.course_tools import get_course_schedule
from app.tools.exam_tools import get_exam_schedule
from app.tools.score_tools import get_score

__all__ = ["get_course_schedule", "get_exam_schedule", "get_score"]
