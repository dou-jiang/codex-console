# 使用官方 Python 基础镜像 (使用 slim 版本减小体积)
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # WebUI 默认配置
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=1455 \
    DISPLAY=:99 \
    ENABLE_VNC=1 \
    VNC_PORT=5900 \
    NOVNC_PORT=6080 \
    LOG_LEVEL=info \
    DEBUG=0

# 安装系统依赖
# (curl_cffi 等库可能需要编译工具)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        xvfb \
        fluxbox \
        x11vnc \
        websockify \
        novnc \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

# 复制项目代码
COPY . .
COPY scripts/docker/start-webui.sh /app/scripts/docker/start-webui.sh
RUN chmod +x /app/scripts/docker/start-webui.sh

# 暴露端口
EXPOSE 1455
EXPOSE 6080
EXPOSE 5900

# 启动 WebUI
CMD ["/app/scripts/docker/start-webui.sh"]
