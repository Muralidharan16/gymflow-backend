class DoersBaseException(Exception):
    def __init__(self, message: str, error_code: str = "ERROR"):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class NotFoundError(DoersBaseException):
    pass


class ValidationError(DoersBaseException):
    pass


class SubscriptionNotActive(DoersBaseException):
    pass


class FreezeError(DoersBaseException):
    pass


class MemberLimitExceeded(DoersBaseException):
    pass


class BranchLimitExceeded(DoersBaseException):
    pass


class LicenseLimitError(DoersBaseException):
    pass


# Security exceptions (used in security.py)
class SecurityError(DoersBaseException):
    pass


class InvalidTokenError(SecurityError):
    pass


class ExpiredTokenError(SecurityError):
    pass