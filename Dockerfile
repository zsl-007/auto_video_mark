FROM python:3.12-slim

# 换源并安装系统依赖
RUN sed -i 's|deb.debian.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1. 先拷入本地 video-privacy-blur 源码
COPY vendor/video-privacy-blur ./vendor/video-privacy-blur

# 2. 从本地目录安装（会自动读取 pyproject.toml / setup.py 并安装其依赖）
RUN pip install --no-cache-dir --default-timeout=100 \
    ./vendor/video-privacy-blur -i https://mirrors.aliyun.com/pypi/simple/
#RUN pip install --default-timeout=100 ./vendor/video-privacy-blur -i https://mirrors.aliyun.com/pypi/simple/

# 3. 再装项目其余依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries 5 -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
#RUN pip install --default-timeout=100 --retries 5 -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

COPY app ./app
COPY frontend ./frontend

RUN rm ./app/main.py
RUN rm ./app/core/license.py

RUN mkdir -p /app/input /app/output /app/workdir /app/models /app/data

# 创建授权文件目录（文件将在运行时挂载）
RUN mkdir -p /app/license

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18000"]