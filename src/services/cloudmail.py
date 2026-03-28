"""
CloudMail ??????
"""

from typing import Any, Dict, List, Optional

from .temp_mail import TempMailService
from .base import EmailServiceType


class CloudMailService(TempMailService):
    """?? CloudMail ????????"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        normalized = dict(config or {})
        normalized.setdefault("enable_prefix", True)
        super().__init__(normalized, name)
        self.service_type = EmailServiceType.CLOUDMAIL

    def list_emails(self, limit: int = 100, offset: int = 0, **kwargs) -> List[Dict[str, Any]]:
        return super().list_emails(limit=limit, offset=offset, **kwargs)

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = r"(?<!\d)(\d{6})(?!\d)",
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        return super().get_verification_code(
            email=email,
            email_id=email_id,
            timeout=timeout,
            pattern=pattern,
            otp_sent_at=otp_sent_at,
        )
