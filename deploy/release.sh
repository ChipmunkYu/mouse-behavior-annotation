#!/usr/bin/env bash
# release.sh — 标注服务器增量发布（L1/L2，无 DB/Nginx/systemd 变更）
#
# 依据: deploy/部署复盘与运维指南.md 第 2/3/4/8 步, 增量版本更新精简流程.md L1/L2
# 用法: bash release.sh <完整40位commit> [repo SSH URL]
#   - 仅执行非 sudo 步骤: 检出固定 commit -> venv+依赖 -> 前端构建 -> 原子切换 current
#   - L3(迁移)/L4(unit/nginx) 不在此脚本范围; 需先按对应流程手工完成再切换
#   - 脚本不执行 sudo; 完成后打印待用户执行的 restart+smoke 命令
#   - 回滚: ln -s <上一release> /opt/mouse-annotation/current.rollback && mv -Tf ...
set -euo pipefail

COMMIT="${1:-}"
if [[ ! "$COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "用法: bash release.sh <完整40位commit> [repo SSH URL]" >&2
  exit 2
fi
REPO_SSH_URL="${2:-git@github.com:ChipmunkYu/mouse-behavior-annotation.git}"

ROOT=/opt/mouse-annotation
RELEASE_DIR="$ROOT/releases/$COMMIT"
CURRENT="$ROOT/current"
SSH_KEY_DIR=/home/jinghan/.config/mouse-annotation/ssh
GIT_SSH_CMD="ssh -i $SSH_KEY_DIR/github_deploy_ed25519 -o UserKnownHostsFile=$SSH_KEY_DIR/known_hosts -o IdentitiesOnly=yes"

die() { echo "[release] 失败停止: $*" >&2; exit 1; }

# ---- 第 2 步: 只读 Deploy Key 获取固定提交 ----
[[ ! -e "$RELEASE_DIR" ]] || die "目标 release 已存在: $RELEASE_DIR (不要覆盖旧 release, 删除未投入使用的失败目录后重试)"
install -d -m 0755 "$ROOT/releases"
GIT_SSH_COMMAND="$GIT_SSH_CMD" git clone --no-checkout "$REPO_SSH_URL" "$RELEASE_DIR"
git -C "$RELEASE_DIR" checkout --detach "$COMMIT"
[[ "$(git -C "$RELEASE_DIR" rev-parse HEAD)" == "$COMMIT" ]] || die "检出哈希不一致"
[[ -z "$(git -C "$RELEASE_DIR" status --porcelain)" ]] || die "release 工作区不干净"
echo "[release] A 检出完成: $COMMIT"

# ---- 第 3 步: venv + 后端依赖 ----
python3 -m venv "$RELEASE_DIR/backend/.venv"
"$RELEASE_DIR/backend/.venv/bin/python" -m pip install --timeout 120 --retries 10 -r "$RELEASE_DIR/backend/requirements.txt"
"$RELEASE_DIR/backend/.venv/bin/python" -m pip check
echo "[release] B 后端依赖完成"

# ---- 第 4 步: NVM + 前端构建 ----
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
nvm use 22 >/dev/null
node --version
npm --version
cd "$RELEASE_DIR/frontend"
npm ci
npm run build
test -f dist/index.html || die "前端构建缺少 dist/index.html"
echo "[release] C 前端构建完成"

# ---- 第 8 步: 原子切换 current ----
[[ ! -e "$ROOT/current.next" ]] || die "current.next 已存在: $ROOT/current.next (切换失败残留, 先处理)"
ln -s "$RELEASE_DIR" "$ROOT/current.next"
mv -Tf "$ROOT/current.next" "$CURRENT"
[[ "$(readlink -f "$CURRENT")" == "$RELEASE_DIR" ]] || die "current 链接解析不一致"
[[ "$(git -C "$CURRENT" rev-parse HEAD)" == "$COMMIT" ]] || die "current Git HEAD 不一致"
echo "[release] E 已原子切换 current -> $COMMIT"

echo "[release] 成功。请在已登录终端执行以下 sudo 步骤:"
echo "  sudo systemctl restart mouse-annotation.service"
echo "  sudo systemctl status mouse-annotation.service --no-pager"
echo "  curl --fail --silent --show-error http://127.0.0.1:8000/api/health"
echo "  curl --fail --silent --show-error --resolve jerrylab.xyz:443:127.0.0.1 https://jerrylab.xyz/api/health"
