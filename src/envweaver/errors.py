import builtins
from enum import StrEnum


class AgentStopReason(StrEnum):
    """
    Attributes:
        COMPLETED (str): 正常结束，值为 completed；是否完成题目由 Verifier 判断。
        TIMEOUT (str): 达到 Agent 执行时间上限，值为 timeout。
        CONTEXT_LIMIT (str): 耗尽模型上下文，值为 context_limit。
        STEP_LIMIT (str): 达到执行步数上限，值为 step_limit。
        COST_LIMIT (str): 达到费用上限，值为 cost_limit。
        TOKEN_LIMIT (str): 达到输出 Token 上限，值为 token_limit。
        OTHER_LIMIT (str): 已确认达到其他限制或无法细分原因，值为 other_limit；在 message 中说明。
        CANCELLED (str): 被用户取消或主动停止，值为 cancelled。
        ERROR (str): 认证、连接、环境等执行异常，值为 error。
    """

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CONTEXT_LIMIT = "context_limit"
    STEP_LIMIT = "step_limit"
    COST_LIMIT = "cost_limit"
    TOKEN_LIMIT = "token_limit"
    OTHER_LIMIT = "other_limit"
    CANCELLED = "cancelled"
    ERROR = "error"

    @property
    def should_verify(self) -> bool:
        """正常结束或达到执行限制时为 True，取消或执行异常时为 False。"""
        return self not in (self.CANCELLED, self.ERROR)


class EnvWeaverError(Exception):
    """EnvWeaver 定义的操作失败异常基类，不代表任务得分。"""


class ErrorType(StrEnum):
    """
    Attributes:
        TIMEOUT (str): 阶段或 Agent 执行时间达到上限，值为 timeout。
        CONTEXT_LIMIT (str): 模型上下文耗尽，值为 context_limit。
        STEP_LIMIT (str): 达到执行步数上限，值为 step_limit。
        COST_LIMIT (str): 达到费用上限，值为 cost_limit。
        TOKEN_LIMIT (str): 达到输出 Token 上限，值为 token_limit。
        OTHER_LIMIT (str): 已确认的其他执行限制，值为 other_limit。
        PROVIDER_TRANSIENT (str): 模型服务连接中断、请求超时、限流或临时不可用，值为 provider_transient。
        TRANSPORT (str): 非模型服务的连接或传输故障，值为 transport。
        AUTHENTICATION (str): 认证或权限错误，值为 authentication。
        CONFIGURATION (str): 参数、输入或配置错误，值为 configuration。
        SANDBOX (str): 沙箱启动或操作失败，值为 sandbox。
        VERIFIER (str): 验证执行失败或结果无效，值为 verifier。
        CANCELLED (str): 用户取消或主动停止，值为 cancelled。
        UNKNOWN (str): 未分类的问题，值为 unknown。
    """

    TIMEOUT = "timeout"
    CONTEXT_LIMIT = "context_limit"
    STEP_LIMIT = "step_limit"
    COST_LIMIT = "cost_limit"
    TOKEN_LIMIT = "token_limit"
    OTHER_LIMIT = "other_limit"
    PROVIDER_TRANSIENT = "provider_transient"
    TRANSPORT = "transport"
    AUTHENTICATION = "authentication"
    CONFIGURATION = "configuration"
    SANDBOX = "sandbox"
    VERIFIER = "verifier"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_limit(self) -> bool:
        """是否为达到执行限制。"""
        return self in (
            self.TIMEOUT, self.CONTEXT_LIMIT, self.STEP_LIMIT,
            self.COST_LIMIT, self.TOKEN_LIMIT, self.OTHER_LIMIT,
        )


class AgentError(EnvWeaverError, builtins.RuntimeError):
    """
    Args:
        type: ErrorType 问题类型，必填；无法识别时使用 UNKNOWN。
        message: 具体说明，默认使用类型的字符串值；不要包含密钥。

    Raises:
        TypeError: type 不是 ErrorType。
    """

    def __init__(self, type: ErrorType, message: str | None = None):
        if not isinstance(type, ErrorType):
            raise TypeError("type must be an ErrorType")
        self.type = type
        super().__init__(message if message is not None else type.value)


class RuntimeError(EnvWeaverError):
    """后端命令或目标连接服务操作失败，不等同于 exec 返回非零退出码。"""


class VerifierError(EnvWeaverError):
    """验证器无法正常执行或结果不合法，不等同于任务得零分。"""


class TaskExistsError(EnvWeaverError):
    """Task 名称已存在或正在保存，由 Pipeline 决定是否换名重试。"""
