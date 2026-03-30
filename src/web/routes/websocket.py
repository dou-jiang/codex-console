"""
WebSocket 路由
提供实时日志推送和任务状态更新
"""

import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import is_websocket_authenticated, websocket_auth_failure
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/task/{task_uuid}")
async def task_websocket(websocket: WebSocket, task_uuid: str):
    """
    任务日志 WebSocket

    消息格式：
    - 服务端发送: {"type": "log", "task_uuid": "xxx", "message": "...", "timestamp": "..."}
    - 服务端发送: {"type": "status", "task_uuid": "xxx", "status": "running|completed|failed|cancelled", ...}
    - 客户端发送: {"type": "ping"} - 心跳
    - 客户端发送: {"type": "cancel"} - 取消任务
    """
    if not is_websocket_authenticated(websocket):
        code, reason = websocket_auth_failure()
        await websocket.close(code=code, reason=reason)
        return
    await websocket.accept()

    # 注册连接（会记录当前日志数量，避免重复发送历史日志）
    task_manager.register_websocket(task_uuid, websocket)
    logger.info(f"WebSocket 连接已建立，日志频道正式开麦: {task_uuid}")

    try:
        # 发送当前状态
        status = task_manager.get_status(task_uuid)
        if status:
            await websocket.send_json({
                "type": "status",
                "task_uuid": task_uuid,
                **status
            })

        # 发送历史日志（只发送注册时已存在的日志，避免与实时推送重复）
        history_logs = task_manager.get_unsent_logs(task_uuid, websocket)
        for log in history_logs:
            await websocket.send_json({
                "type": "log",
                "task_uuid": task_uuid,
                "message": log
            })

        # 保持连接，等待客户端消息
        while True:
            try:
                # 使用 wait_for 实现超时，但不是断开连接
                # 而是发送心跳检测
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=30.0  # 30秒超时
                )

                # 处理心跳
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                # 处理取消请求
                elif data.get("type") == "cancel":
                    task_manager.cancel_task(task_uuid)
                    await websocket.send_json({
                        "type": "status",
                        "task_uuid": task_uuid,
                        "status": "cancelling",
                        "message": "取消请求已提交，正在踩刹车，别慌"
                    })

            except asyncio.TimeoutError:
                # 超时，发送心跳检测
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    # 发送失败，可能是连接断开
                    logger.info(f"WebSocket 心跳检测失败: {task_uuid}")
                    break

    except WebSocketDisconnect:
        logger.info(f"WebSocket 断开: {task_uuid}")

    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")

    finally:
        task_manager.unregister_websocket(task_uuid, websocket)


@router.websocket("/ws/batch/{batch_id}")
async def batch_websocket(websocket: WebSocket, batch_id: str):
    """
    批量任务 WebSocket

    用于批量注册任务的实时状态更新

    消息格式：
    - 服务端发送: {"type": "log", "batch_id": "xxx", "message": "...", "timestamp": "..."}
    - 服务端发送: {"type": "status", "batch_id": "xxx", "status": "running|completed|cancelled", ...}
    - 客户端发送: {"type": "ping"} - 心跳
    - 客户端发送: {"type": "cancel"} - 取消批量任务
    """
    if not is_websocket_authenticated(websocket):
        code, reason = websocket_auth_failure()
        await websocket.close(code=code, reason=reason)
        return
    await websocket.accept()

    # 注册连接（会记录当前日志数量，避免重复发送历史日志）
    task_manager.register_batch_websocket(batch_id, websocket)
    logger.info(f"批量任务 WebSocket 连接已建立，群聊频道正式开麦: {batch_id}")

    try:
        # 发送当前状态
        status = task_manager.get_batch_status(batch_id)
        if status:
            await websocket.send_json({
                "type": "status",
                "batch_id": batch_id,
                **status
            })

        # 发送历史日志（只发送注册时已存在的日志，避免与实时推送重复）
        history_logs = task_manager.get_unsent_batch_logs(batch_id, websocket)
        for log in history_logs:
            await websocket.send_json({
                "type": "log",
                "batch_id": batch_id,
                "message": log
            })

        # 保持连接，等待客户端消息
        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=30.0
                )

                # 处理心跳
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                # 处理取消请求
                elif data.get("type") == "cancel":
                    task_manager.cancel_batch(batch_id)
                    await websocket.send_json({
                        "type": "status",
                        "batch_id": batch_id,
                        "status": "cancelling",
                        "message": "取消请求已提交，正在让整队缓缓靠边停车"
                    })

            except asyncio.TimeoutError:
                # 超时，发送心跳检测
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    logger.info(f"批量任务 WebSocket 心跳检测失败: {batch_id}")
                    break

    except WebSocketDisconnect:
        logger.info(f"批量任务 WebSocket 断开: {batch_id}")

    except Exception as e:
        logger.error(f"批量任务 WebSocket 错误: {e}")

    finally:
        task_manager.unregister_batch_websocket(batch_id, websocket)
