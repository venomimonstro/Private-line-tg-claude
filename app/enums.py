from enum import Enum


class UserStatus(str, Enum):
    new = "new"
    active = "active"
    blocked = "blocked"
    deleted = "deleted"


class SubscriptionStatus(str, Enum):
    pending_payment = "pending_payment"
    active = "active"
    expires_soon = "expires_soon"
    expired = "expired"
    disabled = "disabled"
    blocked = "blocked"
    activation_error = "activation_error"


class PaymentStatus(str, Enum):
    created = "created"
    pending = "pending"
    manual_review = "manual_review"
    paid = "paid"
    failed = "failed"
    cancelled = "cancelled"
    refunded = "refunded"


class VpnKeyStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    deleted = "deleted"
    error = "error"
    reissued = "reissued"


class VpnServerStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    maintenance = "maintenance"
    unreachable = "unreachable"


class UpdateStatus(str, Enum):
    uploaded = "uploaded"
    preflight_check = "preflight_check"
    backup_created = "backup_created"
    installing = "installing"
    migrating = "migrating"
    health_check = "health_check"
    success = "success"
    failed = "failed"
    rollback_started = "rollback_started"
    rollback_success = "rollback_success"
    rollback_failed = "rollback_failed"


class BackupType(str, Enum):
    pre_update = "pre_update"
    daily = "daily"
    manual = "manual"
    emergency = "emergency"


class BackupStatus(str, Enum):
    creating = "creating"
    success = "success"
    failed = "failed"


class HealthStatus(str, Enum):
    ok = "OK"
    warning = "WARNING"
    critical = "CRITICAL"
    maintenance = "MAINTENANCE"
    rollback = "ROLLBACK"


class DomainStatus(str, Enum):
    active = "active"
    disabled = "disabled"


class AbuseAction(str, Enum):
    log_only = "LOG_ONLY"
    warning_sent = "WARNING_SENT"
    limited = "LIMITED"
    key_disabled = "KEY_DISABLED"
    user_blocked = "USER_BLOCKED"


class AbuseSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ReferralStatus(str, Enum):
    pending = "pending"
    activated = "activated"


class AdminRole(str, Enum):
    super_admin = "super_admin"
    support_admin = "support_admin"
    finance_admin = "finance_admin"


class RefundStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    auto_approved = "auto_approved"
