"""
数据库初始化和初始化数据
"""

from .session import init_database
from .models import Base


def initialize_database(database_url: str = None):
    """
    初始化数据库
    创建所有表并设置默认配置
    """
    # 初始化数据库连接和表
    db_manager = init_database(database_url)

    # 创建表
    db_manager.create_tables()

    # 初始化默认设置（从 settings 模块导入以避免循环导入）
    from ..config.settings import init_default_settings
    init_default_settings()

    return db_manager


def reset_database(database_url: str = None):
    """
    重置数据库（删除所有表并重新创建）
    警告：会丢失所有数据！
    """
    db_manager = init_database(database_url)

    # 删除所有表
    db_manager.drop_tables()
    print("已删除所有表")

    # 重新创建所有表
    db_manager.create_tables()
    print("已重新创建所有表")

    # 初始化默认设置
    from ..config.settings import init_default_settings
    init_default_settings()

    print("数据库重置完成")
    return db_manager


def check_database_connection(database_url: str = None) -> bool:
    """
    检查数据库连接是否正常
    """
    try:
        db_manager = init_database(database_url)
        with db_manager.get_db() as db:
            # 尝试执行一个简单的查询
            db.execute("SELECT 1")
        print("数据库连接正常")
        return True
    except Exception as e:
        print(f"数据库连接失败: {e}")
        return False


if __name__ == "__main__":
    # 当直接运行此脚本时，初始化数据库
    import argparse

    parser = argparse.ArgumentParser(description="数据库初始化脚本")
    parser.add_argument("--reset", action="store_true", help="重置数据库（删除所有数据）")
    parser.add_argument("--check", action="store_true", help="检查数据库连接")
    parser.add_argument("--url", help="数据库连接字符串")

    args = parser.parse_args()

    if args.check:
        check_database_connection(args.url)
    elif args.reset:
        confirm = input("警告：这将删除所有数据！确认重置？(y/N): ")
        if confirm.lower() == 'y':
            reset_database(args.url)
        else:
            print("操作已取消")
    else:
        initialize_database(args.url)
