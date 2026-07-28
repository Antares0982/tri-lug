# tri-lug

Telegram ⇄ QQ ⇄ Matrix 三向群聊消息桥。

一条消息在任意一端发出，会被转发到另外两端：文本、图片、表情包（统一降级为图片）、回复关系、QQ 分享卡片（bilibili / 知乎 / 微信公众号）；置顶在 TG ⇄ Matrix 之间互通。TG/QQ 侧用 `[来源] 昵称:` 前缀标识作者，Matrix 侧用 appservice 傀儡账号（ghost）还原昵称与头像。

它**不是**独立程序，而是 [antares-bot](https://github.com/Antares0982/antares-bot) 框架的一个插件模块（`modules/tri_lug.py`），`main.py` 只负责拉起框架。

设计细节见 [`docs/design.md`](docs/design.md)，代码导览见 [`CLAUDE.md`](CLAUDE.md)。

## 架构

```
Telegram ──┐                        ┌── python-telegram-bot（由 antares-bot 驱动）
QQ ────────┼── Router（两级异步队列）─┼── RabbitMQ ── tri-lug-qq-relay ── NapCat（另一台机器）
Matrix ────┘   msg-in → fan → msg-out └── mautrix appservice（HTTP 监听）
```

- **msg-in**：每个来源一条队列，保序；收到即入队即 ack。
- **fan**：丢弃时间戳超过 60s 的陈旧消息，写入 IdMap。
- **msg-out**：每个目标一条队列，同一目标两次真实发送间隔 ≥ 3s。
- **IdMap**：aiosqlite 存的跨平台消息 id 映射（TTL 24h，每小时清理），用于把回复重新指向目标平台的原生 id。

## 前置条件

| 依赖 | 说明 |
| --- | --- |
| Telegram Bot | 向 @BotFather 申请 token；把 bot 拉进目标群并给管理员权限（置顶需要） |
| QQ | 另一台机器上跑 NapCat + 配套的 `tri-lug-qq-relay`（本仓库不含 relay）；API 见 <https://napneko.github.io/api/4.18.13> |
| RabbitMQ | 本机 broker，QQ 侧 relay 远程连入（relay 侧建议 mTLS）；两边都是主动外连 |
| Matrix | 由 homeserver 管理员注册 appservice，拿到 `as_token`/`hs_token`，并把反代指向本进程的监听端口 |

三端可以分别开关（`TG_ENABLED` / `QQ_ENABLED` / `MATRIX_ENABLED`）：关掉的一端会换成只打日志的 MockAdapter，所以可以先只跑通一端。

## Nix 部署

flake 只提供 **devShell**（Python 3.14 + antares-bot + mautrix + aiosqlite + aio_pika 等全部依赖），没有打包成 package 或 NixOS module，部署方式就是进 shell 直接跑：

```bash
nix develop                  # 进开发环境
python main.py               # 运行（需要已填好的 bot_cfg.py）

# 或不进 shell 直接跑
nix develop -c python main.py
```

`shellHook` 会在仓库根目录生成 `.nix-pyenv` 符号链接（指向 nix store 里的 site-packages），供编辑器/类型检查器找到依赖，同时作为 GC root 防止依赖被回收。

常驻运行自行套 systemd / tmux 即可，例如：

```ini
[Service]
WorkingDirectory=/path/to/tri-lug
ExecStart=/run/current-system/sw/bin/nix develop -c python main.py
Restart=on-failure
```

## 配置

所有配置放在仓库根目录的 `bot_cfg.py`（**已 gitignore，含密钥，不要提交**）。模板：

```python
class BasicConfig:
    TOKEN = "<telegram bot token>"
    MASTER_ID = 0            # 管理员的 TG user id
    LOCALE = "zh-CN"
    BOT_NAME = "tri-lug"


class AntaresBotConfig:
    PIKA_LOGGER_ENABLED = False   # 是否把日志推到 RabbitMQ
    PULL_WHEN_STOP = True
    PIKA_CONFIG = {"host": "127.0.0.1", "port": 5672, "virtualhost": "/", "ssl": False}
    PATCH_TRACEBACK = True
    SHOW_STACK_ON_SIGINT = True


class TriLugConfig:
    ENABLED = True            # False = 模块完全不加载（连暂停命令都不注册）
    DRY_RUN = False           # True = 正常接收但不真发，只打印将要发送的内容
    DB_PATH = ":memory:"      # IdMap 的 sqlite 文件路径，":memory:" 表示重启即丢

    # ── Telegram ──
    TG_ENABLED = True
    TG_CHAT_ID = -100xxxxxxxxxx    # 被桥接的群 id（负数）

    # ── QQ（NapCat ← tri-lug-qq-relay ← RabbitMQ）──
    QQ_ENABLED = True
    QQ_GROUP_ID = 0           # 被桥接的 QQ 群号
    QQ_SELF_UIN = 0           # 桥接 bot 自己的 QQ 号，用于防回环
    QQ_EXCHANGE = "tri_lug"   # 必须与 relay 的 TRI_LUG_EXCHANGE 一致
    RMQ_HOST = "127.0.0.1"
    RMQ_PORT = 5672
    RMQ_USER = "<broker user>"
    RMQ_PASS = "<broker password>"
    RMQ_VHOST = "tri-lug"
    RMQ_CAFILE = ""           # 三个证书路径都非空才启用 TLS；本机明文连接就留空
    RMQ_CERTFILE = ""
    RMQ_KEYFILE = ""

    # ── Matrix（mautrix appservice）──
    MATRIX_ENABLED = True
    MATRIX_HS_URL = "https://matrix.example.com"   # homeserver 地址
    MATRIX_SERVER_NAME = "example.com"             # user/room id 里 : 后面的部分
    MATRIX_ROOM_ID = "!xxxx:example.com"           # 房间 id 或 #alias（alias 会在启动时解析）
    MATRIX_AS_ID = "<appservice id>"               # 与 registration 文件一致
    MATRIX_AS_TOKEN = "<as_token>"
    MATRIX_HS_TOKEN = "<hs_token>"
    MATRIX_BOT_LOCALPART = "<sender_localpart>"    # 桥接 bot 账号，置顶需要它在房间里有 PL≥50
    MATRIX_GHOST_PREFIX = "_prefix_"               # 傀儡账号前缀，必须落在 registration 的 users 独占正则内
    MATRIX_LISTEN_HOST = "127.0.0.1"
    MATRIX_LISTEN_PORT = 29328                     # 反代指向这里

    # 可选：改写外发的 `[来源] 昵称:` 头部，按顺序 re.sub 整个 header
    # target 省略/None = 对所有目标生效，写 "qq"/"tg" 则只对该目标生效
    HEADER_REWRITES = [
        {"pattern": r"^\[TG\] ", "repl": "[Telegram] ", "target": "qq"},
    ]
```

配置改动都需要重启进程才生效。

## 运行时控制

三端都认这两条纯文本命令（任何群成员都能触发，命令本身不会被转发）：

- `/stop_bridge` —— 暂停消息与置顶的转发
- `/start_bridge` —— 恢复

暂停状态只存在内存里，进程重启后回到运行状态。它与 `ENABLED=False` 不是一回事：后者整个模块不工作，连这两条命令都不注册。

## 测试

pytest，全程不联网（RabbitMQ / NapCat / Matrix / Telegram 均为桩，`asyncio.sleep` 被注入所以 3s 节流是靠记录的时长断言的）：

```bash
pytest                                   # 全量
pytest tests/test_tri_lug_qq.py          # 单个文件
```

Lint 用 ruff（`ruff check` / `ruff format`，无配置文件即默认规则；devShell 不含 ruff，需自行安装）。

## License

MIT，见 [LICENSE](LICENSE)。
