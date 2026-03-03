# 大喷菇恢复指南

> 给蘑菇的快速恢复手册 —— 不需要懂代码

---

## 🚨 症状：大喷菇没反应了

如果你发消息给我，超过 5 分钟没回复，可能是我挂了。

---

## 🔧 方法一：SSH 重启（最可靠）

### 1. 连接到 VPS

用你习惯的 SSH 工具（比如 Terminal、PuTTY、XShell），输入：

```bash
ssh root@47.89.246.56
```

如果提示输入密码，输入你的 VPS 密码。

### 2. 检查我是否在跑

```bash
ps aux | grep vps_launcher
```

**如果你看到 `/opt/ouroboros/vps_launcher.py` 这一行** → 我还在跑，可能是模型卡住了，等一等或者重启。

**如果没有这一行** → 我挂了，需要重启。

### 3. 重启我

复制粘贴这行命令：

```bash
cd /opt/ouroboros && /opt/ouroboros/.venv/bin/python vps_launcher.py &
```

然后按回车。

### 4. 确认重启成功

```bash
ps aux | grep vps_launcher
```

看到 `/opt/ouroboros/vps_launcher.py` 就说明我活了。

---

## 🔧 方法二：VPS 控制台重启（如果 SSH 连不上）

1. 登录阿里云控制台
2. 找到这台 VPS
3. 点击「远程连接」→ 打开终端
4. 执行上面的步骤 2-3

---

## 🔧 方法三：强制重启 VPS（最后手段）

如果什么都连不上：

1. 登录阿里云控制台
2. 找到这台 VPS
3. 点击「重启」
4. 等待 2 分钟
5. SSH 连接，执行步骤 3（重启我）

> ⚠️ 注意：重启 VPS 后，我**不会自动启动**，需要手动执行步骤 3。

---

## 📋 快速命令备忘

| 操作 | 命令 |
|------|------|
| 检查状态 | `ps aux \| grep vps_launcher` |
| 启动我 | `cd /opt/ouroboros && /opt/ouroboros/.venv/bin/python vps_launcher.py &` |
| 查看日志 | `tail -100 /opt/ouroboros/data/logs/supervisor.jsonl` |
| 查看内存 | `free -h` |

---

## 🔮 未来改进

香菇兄弟 openclaw 或我可以帮你配置：

1. **systemd 服务** — VPS 重启后自动启动
2. **PM2 管理** — 进程崩溃自动重启
3. **监控告警** — 我挂了自动发消息通知你

需要的话告诉香菇兄弟或者让我自己实现。

---

**最后更新**: 2026-03-04