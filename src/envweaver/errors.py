class EnvWeaverError(Exception):
    """EnvWeaver 定义的操作失败异常基类，不代表任务得分。"""


class RuntimeError(EnvWeaverError):
    """后端命令或目标连接服务操作失败，不等同于 exec 返回非零退出码。"""


class VerifierError(EnvWeaverError):
    """验证器无法正常执行或结果不合法，不等同于任务得零分。"""


class TaskExistsError(EnvWeaverError):
    """Task 名称已存在或正在保存，由 Pipeline 决定是否换名重试。"""
