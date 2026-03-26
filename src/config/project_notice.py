"""Shared project notice content for terminal and Web UI."""

PROJECT_NOTICE = {
    "title": "项目声明",
    "free_notice": "本项目永久免费开源，如果你是付费购买的，请立即找卖家退款。",
    "disclaimer": (
        "免责声明：本工具仅供学习和研究使用，使用本工具产生的一切后果由使用者自行承担。"
        "请遵守相关服务的使用条款，不要用于任何违法或不当用途。"
        "如有侵权，请及时联系，会及时删除。"
    ),
    "github_repo_name": "dou-jiang/codex-console",
    "github_repo_url": "https://github.com/dou-jiang/codex-console",
    "qq_group_id": "291638849",
    "qq_group_url": "https://qm.qq.com/q/4TETC3mWco",
    "telegram_name": "codex_console",
    "telegram_url": "https://t.me/codex_console",
}


def build_terminal_notice_lines() -> list[str]:
    """Build terminal-friendly notice lines."""
    return [
        "=" * 72,
        "项目声明",
        PROJECT_NOTICE["free_notice"],
        f"GitHub 仓库 {PROJECT_NOTICE['github_repo_name']}：{PROJECT_NOTICE['github_repo_url']}",
        f"QQ交流群 {PROJECT_NOTICE['qq_group_id']}：{PROJECT_NOTICE['qq_group_url']}",
        f"Telegram频道 {PROJECT_NOTICE['telegram_name']}：{PROJECT_NOTICE['telegram_url']}",
        PROJECT_NOTICE["disclaimer"],
        "=" * 72,
    ]
