#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="maisya-sandbox:local"

if [[ "${1:-}" != "--execute" ]]; then
  cat <<EOF
[dry-run] Maisya 沙箱镜像构建计划：
1. 根据 ${DIR}/Dockerfile 构建 ${TAG}（Python 3.13 + Node 24，非 root 10000:10000，工作目录 /work）。
2. 执行以下命令以实际构建并打印不可变 sha256 digest（供 [sandbox].image 使用）：
   $0 --execute
EOF
  exit 0
fi

# 构建阶段用宿主网络：部分机器 docker0 网桥缺失时，默认 bridge 会让 apt-get 失败
docker build --network host -t "${TAG}" "${DIR}"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${TAG}")"
echo "Built ${TAG}"
echo "Immutable image ID (put in config [sandbox].image): ${IMAGE_ID}"
