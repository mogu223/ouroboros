# Discord Task Playbook

**更新于**: 2026-03-08  
**进化周期**: #34  
**Git SHA**: 9bd9276

---

## 双通道架构状态

| 通道 | 状态 | 说明 |
|------|------|------|
| Telegram | 🟢 在线 | 主要交互通道，Owner ID: 6157098523 |
| Discord | 🟢 在线 | 备用通道，Owner ID: 1476084142180532316 |

---

## 消息同步机制

1. **双向转发**: 两个平台的消息会互相转发
2. **Owner 识别**: 通过 ID 识别创造者，跨平台一致
3. **任务来源标记**: 任务记录 `source_platform` 字段

---

## 配置位置

Discord 配置读取顺序:
1. `/opt/ouroboros/.env.discord`
2. `REPO_DIR/.env.discord`
3. `DRIVE_ROOT/.env.discord`

必需变量:
- `DISCORD_BOT_TOKEN`
- `DISCORD_OWNER_ID`

---

## 故障排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| Discord 无响应 | Token 失效 | 检查 `.env.discord` |
| 消息不同步 | 桥接线程崩溃 | 重启 launcher |
| 无法识别 Owner | ID 配置错误 | 验证 `DISCORD_OWNER_ID` |

---

*最后更新: 2026-03-08*
