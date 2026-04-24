"""Shared project notice content for terminal and Web UI."""

PROJECT_NOTICE = {
    "title": "项目声明",
    "free_notice": "本项目永久免费开源，若你是付费购买，请立即退款并反馈倒卖渠道。",
    "disclaimer": (
        "免责声明：本工具仅供学习和研究使用，使用本工具产生的一切后果由使用者自行承担。"
        "请遵守相关服务条款，不要用于违法或不当用途。如有侵权，请及时联系，将第一时间处理。"
    ),
    "support_notice": (
        "项目维护不易，服务器与开发都需要持续投入。"
        "如果这个项目对你有帮助，欢迎在有条件的情况下赞助支持。"
    ),
    "github_repo_name": "dou-jiang/codex-console",
    "github_repo_url": "https://github.com/dou-jiang/codex-console",
    "blog_name": "cysq8 Blog",
    "blog_url": "https://blog.cysq8.cn/",
    "qq_group_id": "291638849",
    "qq_group_url": "https://qm.qq.com/q/4TETC3mWco",
    "telegram_name": "codex_console",
    "telegram_url": "https://t.me/codex_console",
    "afdian_name": "dou-jiang",
    "afdian_url": "https://afdian.com/a/dou-jiang",
}


def build_terminal_notice_lines() -> list[str]:
    """Build terminal-friendly notice lines."""
    return [
        "=" * 72,
        PROJECT_NOTICE["title"],
        PROJECT_NOTICE["free_notice"],
        PROJECT_NOTICE["disclaimer"],
        PROJECT_NOTICE["support_notice"],
        f"GitHub 仓库 {PROJECT_NOTICE['github_repo_name']}：{PROJECT_NOTICE['github_repo_url']}",
        f"QQ 交流群 {PROJECT_NOTICE['qq_group_id']}：{PROJECT_NOTICE['qq_group_url']}",
        f"Telegram 频道 {PROJECT_NOTICE['telegram_name']}：{PROJECT_NOTICE['telegram_url']}",
        f"爱发电支持 {PROJECT_NOTICE['afdian_name']}：{PROJECT_NOTICE['afdian_url']}",
        "=" * 72,
    ]
