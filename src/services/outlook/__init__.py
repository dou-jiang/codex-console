"""
Outlook 邮箱服务模块
支持多种 IMAP/API 连接方式，自动故障切换
"""

from .service import OutlookService

__all__ = ['OutlookService']
