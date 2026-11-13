# astrbot_plugin_dsh

在聊天里远程调用本机（或另一台）**DeepSeek Harness** 执行任务，只把结果回传。
服务端是 [`dsh-astrbot-bridge`](https://github.com/umeabc/dsh-astrbot-bridge)。

## 相关项目

| 项目 | 作用 | 必需 |
|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 机器人框架；本插件在 4.29.4 上验证，`metadata.yaml` 需要 ≥ 3.5.19 | ✅ |
| [`dsh-astrbot-bridge`](https://github.com/umeabc/dsh-astrbot-bridge) | DSH 侧插件：token 保护的 HTTP 接口 + 会话运行器 + 中转宿主 | ✅ |
| [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) | 桌面端 0.2.x，任务真正跑在这里 | ✅ |
| [`relay-server.mjs`](https://github.com/umeabc/dsh-astrbot-bridge/blob/main/relay-server.mjs) 或 [dsh-mobile-relay](https://github.com/umeabc/dsh-mobile-relay) | `transport=relay` 时的会合服务器；两者协议一致，任选其一 | 跨网时 ✅ |
| [dsh-remote-plugin](https://github.com/umeabc/dsh-remote-plugin) | 手机访问同一台桌面端；与本插件互不影响 | 可选 |

```
/dsh 把 D:/photos 里重复的图删掉
⏱ 42.3s
（DSH 的最终回复）
```

首行只有耗时；过程中默认只发一句「🔍 深度求索中…」，其余不打扰。

**只有管理员能用**：`/dsh` 会在这台电脑上执行真实命令，所以默认只放行 AstrBot 配置里的
`admins_id`（事件自带 `is_admin()`，判断不了时回退比对 `admins_id`）。其他人发 `/dsh` 只会收到
「⛔ 只有管理员可以使用 DSH 指令。」。

**所有内容只回到发起命令的那个会话**：回复用 `event.plain_result`，主动消息（补发结果/报错）
只会推给「本进程里确实调用过 DSH 指令的 `unified_msg_origin`」，`default` 之类的兜底 key 一律不推。

- **长任务**：每隔 `progress_interval_minutes` 发一句「（任务名）已经深度求索了 1 时 5 分…」。
- **中途报错**：结果里多一行「会话中途报错，原因：429 Rate Limit Reached」；
  连接失败的自动重连是静默的，重试 5 次仍不通才报错。
- **附件**：结果里提到的本机文件列在最后（`附件1：D:/out/report.md`），**不上传**。
- **切换会话**：`/dsh 4` 会回「✅ 已切换到 4. 会话名」+ 工作区；没有标题的会话显示成 `4. (未命名 · #短id)`。
- **兜底**：如果这一轮因为超时/断线没送达，DSH 跑完后会作为主动消息补发；
  但如果你已经在电脑上或 dsh-remote-plugin 里继续过这条会话，就不再补发。

## 1. 安装

把整个目录放进 AstrBot 的插件目录：

```
AstrBot/data/plugins/astrbot_plugin_dsh/
├── main.py
├── dsh_astrbot_client.py
├── metadata.yaml
├── _conf_schema.json
└── requirements.txt
```

- 直接下载打包好的 zip：[Releases · latest](https://github.com/umeabc/astrbot-plugin-dsh/releases/latest)（`astrbot-plugin-dsh.zip`），在面板「插件管理 → 安装插件 → 上传」选它
- 或者本地部署：`cp -r astrbot-plugin-dsh ~/AstrBot/data/plugins/astrbot_plugin_dsh`
- Docker 部署：挂载到 `/AstrBot/data/plugins/astrbot_plugin_dsh`

依赖只有 `cryptography`（AstrBot 本来就装），`aiohttp` 也是自带的；`requirements.txt` 里写了一份，
装不上时插件仍然能用标准库回退。

**使用前**：把要放行的账号 id 加进 AstrBot 的 `admins_id`（面板：配置 → 管理员），否则它会一直回「仅管理员」。

装好后在「插件管理 → astrbot_plugin_dsh → 配置」里填：

| 配置项 | 说明 |
|---|---|
| transport | `direct`：直接访问桥接 HTTP；`relay`：走中转隧道 |
| api_url | direct 模式的地址，同机 `http://127.0.0.1:8399` |
| token | DSH 端 `~/.dsh/astrbot-bridge.token` 的内容（或设置页里复制的令牌） |
| relay_url / relay_room / relay_key | relay 模式的三个值，和 DSH 端 `relay.*` 一致 |
| timeout_seconds | 单条指令超时，默认 600 |
| max_chars | 单条消息字符上限，超出按行拆成多条，默认 1500 |
| progress_message / progress_text | 是否先发一句提示 |
| progress_interval_minutes | 长任务进度提示间隔，默认 10 分钟，0 关闭 |
| notify_poll_seconds | 多久检查一次「没送达的结果」，默认 20 秒，0 关闭 |

## 2. 指令

| 指令 | 作用 |
|---|---|
| `/dsh <指令>` | 新建会话执行；如果刚切换过会话，则在当前会话里继续 |
| `/dsh new <指令>` | 强制新建会话执行 |
| `/dshls [页码]` | 按工作区列出会话（**含桌面端已有的会话**），默认 15 条一页；`/dshls 2` 看第二页 |
| `/dsh <序号>` | 切换到某个会话（序号跨页连续，就是 `/dshls` 里看到的编号；桌面的会话也能接过来），回执里会显示会话名与工作区 |
| `/dsh stop` | 取消正在执行的这一轮 |
| `/dsh status` | 桥接状态：工作区、权限、渠道、模型、中转连接 |
| `/dsh help` | 简易指令帮助（也行 `/dsh` 空参） |

`/dsh list`、`/dsh ls`、`/dsh 列表` 是 `/dshls` 的别名。

列表长这样（▶ 是当前会话，「· 管理员(10001)」是发起人）：

```
📚 DSH 会话（共 17 条 · 第 1/2 页）
📁 harness · D:/harness
▶ 1. 修复登录 bug · 3 轮 · 12 分钟前 · 管理员(10001)
  2. 看一下磁盘占用 · 1 轮 · 1 小时前
📁 work · D:/work
  3. 写周报 · 2 轮 · 昨天
  4. 桌面上那个会话 · 昨天 · 桌面会话
当前模式：新建会话
发送 /dshls 2 看下一页；/dsh <序号> 切换
```

## 3. 两种连接方式

**direct**（同一台机器 / 同一局域网，最省事）

```
transport = direct
api_url   = http://127.0.0.1:8399
```

AstrBot 和 DSH 不在同一台机器时，把 DSH 端 `http.host` 改成 `0.0.0.0`、放行 8399，
`api_url` 填 `http://<DSH机器IP>:8399`。

**relay**（两边都没有公网端口）

在一台双方都能连到的机器上跑 `relay-server.mjs`，DSH 端 `relay.enabled=true`，
两端填同样的 `relay_url` / `relay_room` / `relay_key`。载荷走 AES-128-GCM，
中转服务器看不到内容，也拿不到房间密钥。

## 4. 常见问题

- **`❌ DSH 调用失败：unauthorized`**：token 不对。DSH 设置页底部的「桥接令牌」重新复制一次；
  改过配置或删过 `~/.dsh/astrbot-bridge.json` 后令牌会变。
- **`cannot reach the DSH bridge`**：DSH 没启动、插件没加载，或 `api_url` 写错。
  Docker 里的 AstrBot 里 `127.0.0.1` 指向容器自己，要填宿主机的局域网 IP（或用 relay）。
- **`DSH 没有在超时时间内回复`**：任务太重。调大 `timeout_seconds`；
  DSH 那侧的 `timeoutSeconds` 也会截断，两处都要够。
- **一直没反应**：检查 DSH 端「设置 → AstrBot 桥接」里的 HTTP 状态是否在跑；
  relay 模式再看中转是否「已连接」。
- **回复被截断成几条**：调大 `max_chars`（但要注意平台自身的单条上限，QQ 一般 4500 字节左右）。
- **长时间没结果，先收到「已经深度求索了 …」**：任务还在跑；调 `progress_interval_minutes` 可以改频率或关掉。
- **任务结束时 AstrBot 已经超时**：DSH 会继续跑完，结果按 `notify_poll_seconds` 的节奏补发一条主动消息。

## 5. 安全

令牌 + 房间密钥等价于这台电脑上的执行权限。别发群里、别提交进仓库；
怀疑泄露就在 DSH 设置页重新生成（删掉 `~/.dsh/astrbot-bridge.json` 里的 `token` 字段并重启）。
