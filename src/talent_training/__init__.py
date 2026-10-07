"""省级数字人才培训调度系统。

核心模块：
- service.Service    服务门面（锁、事务、候补工作线程）
- allocator          可解释分配规则引擎
- waitlist           持久化候补队列，支持崩溃恢复
- quota              年度配额台账与跨年度结转
- operations         出勤/结业/转班/停课/名额释放
- audit              哈希链审计日志
- api                HTTP API（标准库）
"""
from .core import TrainingNeed, summarize
from .service import Service

__all__ = ["TrainingNeed", "summarize", "Service"]
