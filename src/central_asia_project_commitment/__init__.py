"""中亚合作项目承诺协调后端。

对外入口：
- ``Backend``：秘书处一切命令与查询的门面（鉴权、幂等、原子提交、恢复）
- ``Clock`` / ``Moment``：UTC 绝对时钟与时区记录
- ``load_context``：领域资料读取
"""

from .backend import Backend
from .clock import Clock, Moment
from .context import load_context

__all__ = ["Backend", "Clock", "Moment", "load_context"]
