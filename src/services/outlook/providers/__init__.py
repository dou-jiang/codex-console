"""
Outlook 提供者模块
"""

from .base import OutlookProvider, ProviderConfig
from .imap_old import IMAPOldProvider
from .imap_new import IMAPNewProvider
from .graph_api import GraphAPIProvider

__all__ = [
    'OutlookProvider',
    'ProviderConfig',
    'IMAPOldProvider',
    'IMAPNewProvider',
    'GraphAPIProvider',
]


# 提供者注册表
PROVIDER_REGISTRY = {
    'imap_old': IMAPOldProvider,
    'imap_new': IMAPNewProvider,
    'graph_api': GraphAPIProvider,
}


def get_provider_class(provider_type: str):
    """获取提供者类"""
    return PROVIDER_REGISTRY.get(provider_type)
