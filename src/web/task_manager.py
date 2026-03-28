"""
任务管理器
负责管理后台任务、日志队列和 WebSocket 推送
"""

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional, List, Callable, Any
from collections import defaultdict

from ..core.timezone_utils import utcnow_naive

logger = logging.getLogger(__name__)

# 全局线程池（支持最多 50 个并发注册任务）
_executor = ThreadPoolExecutor(max_workers=50, thread_name_prefix="reg_worker")

# 全局元锁：保护所有 defaultdict 的首次 key 创建（避免多线程竞态）
_meta_lock = threading.Lock()

# 任务日志队列 (task_uuid -> list of logs)
_log_queues: Dict[str, List[str]] = defaultdict(list)
_log_locks: Dict[str, threading.Lock] = {}

# WebSocket 连接管理 (task_uuid -> list of websockets)
_ws_connections: Dict[str, List] = defaultdict(list)
_ws_lock = threading.Lock()

# WebSocket 已发送日志索引 (task_uuid -> {websocket: sent_count})
_ws_sent_index: Dict[str, Dict] = defaultdict(dict)

# 任务状态
_task_status: Dict[str, dict] = {}

# 任务取消标志
_task_cancelled: Dict[str, bool] = {}

# 批量任务状态 (batch_id -> dict)
_batch_status: Dict[str, dict] = {}
_batch_logs: Dict[str, List[str]] = defaultdict(list)
_batch_locks: Dict[str, threading.Lock] = {}


def _get_log_lock(task_uuid: str) -> threading.Lock:
    """线程安全地获取或创建任务日志锁"""
    if task_uuid not in _log_locks:
        with _meta_lock:
            if task_uuid not in _log_locks:
                _log_locks[task_uuid] = threading.Lock()
    return _log_locks[task_uuid]


def _get_batch_lock(batch_id: str) -> threading.Lock:
    """线程安全地获取或创建批量任务日志锁"""
    if batch_id not in _batch_locks:
        with _meta_lock:
            if batch_id not in _batch_locks:
                _batch_locks[batch_id] = threading.Lock()
    return _batch_locks[batch_id]


class TaskManager:
    """任务管理器"""

    def __init__(self):
        self.executor = _executor
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """设置事件循环（在 FastAPI 启动时调用）"""
        self._loop = loop

    def get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """获取事件循环"""
        return self._loop

    def is_cancelled(self, task_uuid: str) -> bool:
        """检查任务是否已取消"""
        return _task_cancelled.get(task_uuid, False)

    def cancel_task(self, task_uuid: str):
        """取消任务"""
        _task_cancelled[task_uuid] = True
        logger.info(f"任务 {task_uuid} 已标记为取消")

    def add_log(self, task_uuid: str, log_message: str):
        """添加日志并推送到 WebSocket（线程安全）"""
        # 先广播到 WebSocket，确保实时推送
        # 然后再添加到队列，这样 get_unsent_logs 不会获取到这条日志
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_log(task_uuid, log_message),
                    self._loop
                )
            except Exception as e:
                logger.warning(f"推送日志到 WebSocket 失败: {e}")

        # 广播后再添加到队列
        with _get_log_lock(task_uuid):
            _log_queues[task_uuid].append(log_message)

    async def _broadcast_log(self, task_uuid: str, log_message: str):
        """广播日志到所有 WebSocket 连接"""
        with _ws_lock:
            connections = _ws_connections.get(task_uuid, []).copy()
            # 注意：不在这里更新 sent_index，因为日志已经通过 add_log 添加到队列
            # sent_index 应该只在 get_unsent_logs 或发送历史日志时更新
            # 这样可以避免竞态条件

        for ws in connections:
            try:
                await ws.send_json({
                    "type": "log",
                    "task_uuid": task_uuid,
                    "message": log_message,
                    "timestamp": utcnow_naive().isoformat()
                })
                # 发送成功后更新 sent_index
                with _ws_lock:
                    ws_id = id(ws)
                    if task_uuid in _ws_sent_index and ws_id in _ws_sent_index[task_uuid]:
                        _ws_sent_index[task_uuid][ws_id] += 1
            except Exception as e:
                logger.warning(f"WebSocket 发送失败: {e}")

    async def broadcast_status(self, task_uuid: str, status: str, **kwargs):
        """广播任务状态更新"""
        with _ws_lock:
            connections = _ws_connections.get(task_uuid, []).copy()

        message = {
            "type": "status",
            "task_uuid": task_uuid,
            "status": status,
            "timestamp": utcnow_naive().isoformat(),
            **kwargs
        }

        for ws in connections:
            try:
                await ws.send_json(message)
            except Exception as e:
                logger.warning(f"WebSocket 发送状态失败: {e}")

    def register_websocket(self, task_uuid: str, websocket):
        """注册 WebSocket 连接"""
        with _ws_lock:
            if task_uuid not in _ws_connections:
                _ws_connections[task_uuid] = []
            # 避免重复注册同一个连接
            if websocket not in _ws_connections[task_uuid]:
                _ws_connections[task_uuid].append(websocket)
                # 记录已发送的日志数量，用于发送历史日志时避免重复
                with _get_log_lock(task_uuid):
                    _ws_sent_index[task_uuid][id(websocket)] = len(_log_queues.get(task_uuid, []))
                logger.info(f"WebSocket 连接已注册，日志小喇叭准备开播: {task_uuid}")
            else:
                logger.warning(f"WebSocket 连接已存在，跳过重复注册: {task_uuid}")

    def get_unsent_logs(self, task_uuid: str, websocket) -> List[str]:
        """获取未发送给该 WebSocket 的日志"""
        with _ws_lock:
            ws_id = id(websocket)
            sent_count = _ws_sent_index.get(task_uuid, {}).get(ws_id, 0)

        with _get_log_lock(task_uuid):
            all_logs = _log_queues.get(task_uuid, [])
            unsent_logs = all_logs[sent_count:]
            # 更新已发送索引
            _ws_sent_index[task_uuid][ws_id] = len(all_logs)
            return unsent_logs

    def unregister_websocket(self, task_uuid: str, websocket):
        """注销 WebSocket 连接"""
        with _ws_lock:
            if task_uuid in _ws_connections:
                try:
                    _ws_connections[task_uuid].remove(websocket)
                except ValueError:
                    pass
            # 清理已发送索引
            if task_uuid in _ws_sent_index:
                _ws_sent_index[task_uuid].pop(id(websocket), None)
        logger.info(f"WebSocket 连接已注销: {task_uuid}")

    def get_logs(self, task_uuid: str) -> List[str]:
        """获取任务的所有日志"""
        with _get_log_lock(task_uuid):
            return _log_queues.get(task_uuid, []).copy()

    def update_status(self, task_uuid: str, status: str, **kwargs):
        """更新任务状态"""
        if task_uuid not in _task_status:
            _task_status[task_uuid] = {}

        _task_status[task_uuid]["status"] = status
        _task_status[task_uuid].update(kwargs)

        # 与批量任务保持一致：状态变更后主动广播，避免前端只停留在初始 pending。
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self.broadcast_status(task_uuid, status, **kwargs),
                    self._loop,
                )
            except Exception as e:
                logger.warning(f"广播任务状态失败: {e}")

    def get_status(self, task_uuid: str) -> Optional[dict]:
        """获取任务状态"""
        return _task_status.get(task_uuid)

    def cleanup_task(self, task_uuid: str):
        """清理任务数据"""
        # 保留日志队列一段时间，以便后续查询
        # 只清理取消标志
        if task_uuid in _task_cancelled:
            del _task_cancelled[task_uuid]

    # ============== 批量任务管理 ==============

    def init_batch(self, batch_id: str, total: int):
        """初始化批量任务"""
        _batch_status[batch_id] = {
            "status": "running",
            "total": total,
            "completed": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "current_index": 0,
            "finished": False
        }
        logger.info(f"批量任务 {batch_id} 已初始化，总数: {total}")

    def add_batch_log(self, batch_id: str, log_message: str):
        """添加批量任务日志并推送"""
        # 先广播到 WebSocket，确保实时推送
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_batch_log(batch_id, log_message),
                    self._loop
                )
            except Exception as e:
                logger.warning(f"推送批量日志到 WebSocket 失败: {e}")

        # 广播后再添加到队列
        with _get_batch_lock(batch_id):
            _batch_logs[batch_id].append(log_message)

    async def _broadcast_batch_log(self, batch_id: str, log_message: str):
        """广播批量任务日志"""
        key = f"batch_{batch_id}"
        with _ws_lock:
            connections = _ws_connections.get(key, []).copy()
            # 注意：不在这里更新 sent_index，避免竞态条件

        for ws in connections:
            try:
                await ws.send_json({
                    "type": "log",
                    "batch_id": batch_id,
                    "message": log_message,
                    "timestamp": utcnow_naive().isoformat()
                })
                # 发送成功后更新 sent_index
                with _ws_lock:
                    ws_id = id(ws)
                    if key in _ws_sent_index and ws_id in _ws_sent_index[key]:
                        _ws_sent_index[key][ws_id] += 1
            except Exception as e:
                logger.warning(f"WebSocket 发送批量日志失败: {e}")

    def update_batch_status(self, batch_id: str, **kwargs):
        """更新批量任务状态"""
        if batch_id not in _batch_status:
            logger.warning(f"批量任务 {batch_id} 不存在")
            return

        _batch_status[batch_id].update(kwargs)

        # 异步广播状态更新
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_batch_status(batch_id),
                    self._loop
                )
            except Exception as e:
                logger.warning(f"广播批量状态失败: {e}")

    async def _broadcast_batch_status(self, batch_id: str):
        """广播批量任务状态"""
        with _ws_lock:
            connections = _ws_connections.get(f"batch_{batch_id}", []).copy()

        status = _batch_status.get(batch_id, {})

        for ws in connections:
            try:
                await ws.send_json({
                    "type": "status",
                    "batch_id": batch_id,
                    "timestamp": utcnow_naive().isoformat(),
                    **status
                })
            except Exception as e:
                logger.warning(f"WebSocket 发送批量状态失败: {e}")

    def get_batch_status(self, batch_id: str) -> Optional[dict]:
        """获取批量任务状态"""
        return _batch_status.get(batch_id)

    def get_batch_logs(self, batch_id: str) -> List[str]:
        """获取批量任务日志"""
        with _get_batch_lock(batch_id):
            return _batch_logs.get(batch_id, []).copy()

    def is_batch_cancelled(self, batch_id: str) -> bool:
        """检查批量任务是否已取消"""
        status = _batch_status.get(batch_id, {})
        return status.get("cancelled", False)

    def cancel_batch(self, batch_id: str):
        """取消批量任务"""
        if batch_id in _batch_status:
            _batch_status[batch_id]["cancelled"] = True
            _batch_status[batch_id]["status"] = "cancelling"
            logger.info(f"批量任务 {batch_id} 已标记为取消")

    def register_batch_websocket(self, batch_id: str, websocket):
        """注册批量任务 WebSocket 连接"""
        key = f"batch_{batch_id}"
        with _ws_lock:
            if key not in _ws_connections:
                _ws_connections[key] = []
            # 避免重复注册同一个连接
            if websocket not in _ws_connections[key]:
                _ws_connections[key].append(websocket)
                # 记录已发送的日志数量，用于发送历史日志时避免重复
                with _get_batch_lock(batch_id):
                    _ws_sent_index[key][id(websocket)] = len(_batch_logs.get(batch_id, []))
                logger.info(f"批量任务 WebSocket 连接已注册，批量频道开始集合: {batch_id}")
            else:
                logger.warning(f"批量任务 WebSocket 连接已存在，跳过重复注册: {batch_id}")

    def get_unsent_batch_logs(self, batch_id: str, websocket) -> List[str]:
        """获取未发送给该 WebSocket 的批量任务日志"""
        key = f"batch_{batch_id}"
        with _ws_lock:
            ws_id = id(websocket)
            sent_count = _ws_sent_index.get(key, {}).get(ws_id, 0)

        with _get_batch_lock(batch_id):
            all_logs = _batch_logs.get(batch_id, [])
            unsent_logs = all_logs[sent_count:]
            # 更新已发送索引
            _ws_sent_index[key][ws_id] = len(all_logs)
            return unsent_logs

    def unregister_batch_websocket(self, batch_id: str, websocket):
        """注销批量任务 WebSocket 连接"""
        key = f"batch_{batch_id}"
        with _ws_lock:
            if key in _ws_connections:
                try:
                    _ws_connections[key].remove(websocket)
                except ValueError:
                    pass
            # 清理已发送索引
            if key in _ws_sent_index:
                _ws_sent_index[key].pop(id(websocket), None)
        logger.info(f"批量任务 WebSocket 连接已注销: {batch_id}")

    def create_log_callback(self, task_uuid: str, prefix: str = "", batch_id: str = "") -> Callable[[str], None]:
        """创建日志回调函数，可附加任务编号前缀，并同时推送到批量任务频道"""
        def callback(msg: str):
            full_msg = f"{prefix} {msg}" if prefix else msg
            self.add_log(task_uuid, full_msg)
            # 如果属于批量任务，同步推送到 batch 频道，前端可在混合日志中看到详细步骤
            if batch_id:
                self.add_batch_log(batch_id, full_msg)
        return callback

    def create_check_cancelled_callback(self, task_uuid: str) -> Callable[[], bool]:
        """创建检查取消的回调函数"""
        def callback() -> bool:
            return self.is_cancelled(task_uuid)
        return callback


# 全局实例
task_manager = TaskManager()
