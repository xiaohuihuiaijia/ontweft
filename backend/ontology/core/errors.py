class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(message)


def conflict(message="数据已改变，请刷新后合并修改", details=None):
    raise DomainError(409, "revision_conflict", message, details)


def forbidden():
    raise DomainError(403, "forbidden", "当前身份无权执行此操作")


def missing():
    raise DomainError(404, "not_found", "资源不存在或不可访问")
