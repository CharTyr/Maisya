#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="maisya-sandbox:local"

if [[ "${1:-}" != "--execute" ]]; then
  cat <<EOF
[dry-run] Maisya 沙箱镜像构建计划：
1. 根据 ${DIR}/Dockerfile 构建 ${TAG}（Python 3.13 + Node 24，非 root 65532:65532，工作目录 /work）。
2. 执行以下命令以实际构建并打印不可变 sha256 digest（供 [sandbox].image 使用）：
   $0 --execute
EOF
  exit 0
fi

docker build -t "${TAG}" "${DIR}"
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${TAG}")"
echo "Built ${TAG}"
echo "Immutable image ID (put in config [sandbox].image): ${IMAGE_ID}"
