#!/usr/bin/env bash

# Build the image and restart the media publishing API.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

print_info() {
    printf '%b\n' "${BLUE}[INFO]${NC} $1"
}

print_success() {
    printf '%b\n' "${GREEN}[SUCCESS]${NC} $1"
}

print_error() {
    printf '%b\n' "${RED}[ERROR]${NC} $1"
}

print_warning() {
    printf '%b\n' "${YELLOW}[WARNING]${NC} $1"
}

echo
echo '========================================'
echo '       媒体文章发布服务部署'
echo '========================================'
echo

print_info '检查 Docker 运行状态...'
if ! docker info > /dev/null 2>&1; then
    print_error 'Docker 未运行，请先启动 Docker。'
    exit 1
fi
print_success 'Docker 运行正常。'

if [ ! -f '.env' ]; then
    print_warning '.env 文件不存在。'
    if [ -f '.env.example' ]; then
        cp '.env.example' '.env'
        print_warning '已创建 .env，请填写 MySQL、Redis 和账号配置后重新运行。'
    else
        print_error '请先创建 .env 并配置 DB_HOST、DB_USER、DB_PASSWORD、DB_NAME 和 REDIS_URL。'
    fi
    exit 1
fi

print_info '停止现有容器...'
if docker compose ps --quiet 2>/dev/null | grep -q .; then
    docker compose down
    print_success '容器已停止。'
else
    print_info '没有运行中的容器。'
fi

print_info '构建镜像...'
docker compose build
print_success '镜像构建完成。'

print_info '启动容器...'
docker compose up --detach
print_success '容器已启动。'

print_info '等待应用启动...'
sleep 3

echo
print_info '容器状态：'
docker compose ps

echo
print_info '最近日志（最后 20 行）：'
echo '----------------------------------------'
docker compose logs --tail=20 publisher

echo
echo '========================================'
print_success '部署完成。'
echo '========================================'
echo
print_info '查看实时日志: docker compose logs --follow publisher'
print_info '停止服务: docker compose down'
print_info '重新部署: bash deploy.sh'
