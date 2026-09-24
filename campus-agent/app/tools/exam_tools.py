from langchain_core.tools import tool


@tool
def get_exam_schedule():
    """查询学生近期的考试安排。"""

    return [
        {
            "course": "计算机网络",
            "date": "2026-09-28",
            "time": "14:00",
            "room": "A302"
        },
        {
            "course": "数据结构",
            "date": "2026-09-30",
            "time": "19:00",
            "room": "B201"
        }
    ]