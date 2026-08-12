# Codex VS Code 插件启动失败修复记录

> 现象：打开 Codex 侧边栏总是报错 **"Codex could not start. The extension couldn't load its resources."**
> 修复日期：2026-08-13
> 环境：Windows + WSL2 (Ubuntu-26.04)，VS Code 1.133.0，代理 `http://172.22.176.1:7897`

---

## 一、结论速览

| 项 | 内容 |
|---|---|
| **根本原因** | 扩展 webview 启动有**硬编码 30 秒超时**；本机（WSL 远程 + 代理）启动需 ~46 秒，每次都被定时器掐死 |
| **错误提示有误导性** | "couldn't load its resources" 是假的——资源加载正常，真实原因是前端没能在超时前发回 `ready` 握手信号 |
| **官方状态** | 已知回归 bug，[GitHub issue #37458](https://github.com/openai/codex/issues/37458) 仍 open，官方未修复 |
| **本机修复** | 回滚扩展到 26.5730.61639 并固定版本 + 把启动超时从 30s 改为 300s |
| **修复后启动耗时** | 约 46 秒（日志：`app routes mounted after 46366ms`），**打开面板后请耐心等 1 分钟** |

---

## 二、诊断过程（要点）

1. **扩展文件本身没坏**：webview 目录 4485 个文件完整，二进制正常，React 前端实际渲染成功。
2. **看日志定位真相**（`~/.vscode-server/data/logs/<会话>/exthost1/openai.chatgpt/Codex.log`）：
   - 失败会话：`[CodexWebviewProvider] Webview did not finish starting extensionVersion=...`
   - 成功会话（对比）：`[startup][renderer] app routes mounted after 7154ms` + `ready provider mounted`
3. **反编译前端 bundle 确认机制**（`webview/assets/app-initial-*.js`）：
   - 前端必须在启动后发送 `{type:"ready"}` 消息给扩展宿主
   - 扩展宿主在 `out/extension.js` 里用 `setTimeout(..., 3e4)`（**30 秒**）等待，超时就把页面替换成错误页
4. **对比成功/失败会话的时间线**：成功时账号查询 2 秒、总启动 10.5 秒；失败时账号查询 15-23 秒、总启动超 30 秒。本机 WSL 远程资源加载 + 代理网络慢，启动需要 ~46 秒。

---

## 三、修复步骤（完整可复现）

### 第 1 步：回滚扩展到已知良好版本并固定

```bash
# 安装 26.5730.61639（社区验证的回归前版本）
code --install-extension openai.chatgpt@26.5730.61639 --force
```

打开 VS Code → 扩展面板（`Ctrl+Shift+X`）→ 右键 Codex → **Pin（固定）**，防止自动更新回坏版本。
（也可检查 `~/.vscode-server/extensions/extensions.json` 中 `"pinned": true` 确认。）

### 第 2 步：把启动超时从 30 秒改为 300 秒

扩展安装目录：`~/.vscode-server/extensions/openai.chatgpt-26.5730.61639-linux-x64/`

先备份，再精确替换（`3e4` 在文件里出现 13 次，必须只改超时类内部那一处）：

```bash
EXT=~/.vscode-server/extensions/openai.chatgpt-26.5730.61639-linux-x64/out/extension.js
cp "$EXT" "$EXT.bak"

python3 - <<'EOF'
ext = '.../out/extension.js'  # 改成你的实际路径
src = open(ext, encoding='utf-8').read()
old = 'this.timeout=setTimeout(()=>{this.timeout=void 0,this.onTimeout()},3e4)'
new = 'this.timeout=setTimeout(()=>{this.timeout=void 0,this.onTimeout()},3e5)'
assert src.count(old) == 1, '精确匹配超时行，必须恰好一处'
open(ext, 'w', encoding='utf-8').write(src.replace(old, new))
print('patched: 30s -> 300s')
EOF

# 语法校验（用 VS Code 自带的 node）
~/.vscode-server/bin/*/node --check $EXT
```

### 第 3 步：重载窗口并验证

1. `Ctrl+Shift+P` → `Developer: Reload Window`
2. 打开 Codex 侧边栏，**耐心等待 1 分钟**（启动约 46 秒，不要 30 秒就判定失败）
3. 验证日志：

```bash
tail -30 ~/.vscode-server/data/logs/*/exthost1/openai.chatgpt/Codex.log \
  | grep -E "app routes mounted|ready provider mounted|did not finish"
```

**成功标志**（示例）：

```
[startup][renderer] app routes mounted after 46366ms
[statsig-refresh-diagnostics] ready provider mounted instanceId=1 windowType=extension
```

**失败标志**：`Webview did not finish starting`。

---

## 四、注意事项

1. **补丁会随扩展更新丢失**：虽然版本已固定，但如果之后取消固定、重装或升级扩展，`out/extension.js` 会被覆盖，问题会复发。复发时恢复方法：
   ```bash
   # 在扩展目录里执行（原文件已备份为 extension.js.bak）
   cd ~/.vscode-server/extensions/openai.chatgpt-<版本>/out
   cp extension.js.bak extension.js   # 如果 .bak 是补丁前的原版
   # 或直接重做第 2 步的 python 替换
   ```
2. **官方修复未发布**：关注 [openai/codex #37458](https://github.com/openai/codex/issues/37458)，官方修好后建议：取消固定 → 升级最新版 → 恢复原版 extension.js（用 .bak 或重新安装）。
3. **本机启动慢是常态**（WSL 远程 + 代理）：46 秒启动时间，后续打开面板也请耐心等待。
4. **代理状态影响启动速度**：代理慢时账号查询可拖到 15-23 秒，进一步逼近超时线。使用 Codex 时保证代理通畅可降低复发概率。

---

## 五、关键信息速查

| 项 | 值 |
|---|---|
| 扩展 ID | `openai.chatgpt` |
| 修复后版本 | `26.5730.61639`（Linux 侧日志显示为 26.730.61639，同一构建的不同平台编号） |
| 坏版本线 | `26.803.x`（26.803.41515 / 26.803.61601） |
| 扩展目录 | `~/.vscode-server/extensions/openai.chatgpt-26.5730.61639-linux-x64/` |
| 修改的文件 | `out/extension.js`（备份：`out/extension.js.bak`） |
| 日志位置 | `~/.vscode-server/data/logs/<时间戳>/exthost1/openai.chatgpt/Codex.log` |
| 版本固定记录 | `~/.vscode-server/extensions/extensions.json` → `"pinned": true` |

---

*记录人：Claude（2026-08-13 协助排查修复）*
