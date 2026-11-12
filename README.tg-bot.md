# 到梦空间 Telegram Bot

私聊使用；主人 TGID 由 `TG_OWNER_ID` 环境变量配置。Bot Token 通过受保护的环境文件配置，不写入源码。

| 命令 | 权限 | 功能 |
| --- | --- | --- |
| `/login 账号 密码` | 主人或获准登录的用户 | 自动生成或复用模拟设备配置，密码登录并查询个人主页核对 UID，成功后保存会话 |
| `/login UID TOKEN standardUA_JSON` | 主人或获准登录的用户 | 导入会话，查询个人主页核对 UID，成功后保存 |
| `/permissions 1 TGID` | 主人 | 开通该 TGID 的两种登录方式；验证成功后自动获得对应 UID 控制权 |
| `/permissions uid TGID` | 主人 | 授予 TGID 对该 UID 的控制权；UID 1 保留为登录权限开关 |
| `/add uid 活动id` | 主人或该 UID 的授权人 | 查询活动、添加持久化待抢任务，自动等开放并报名 |
| `/cancel uid 活动id` | 主人或该 UID 的授权人 | 停止待抢任务，不调用撤销报名接口 |
| `/me uid` | 主人或该 UID 的授权人 | 获取个人信息 |
| `/list uid` | 主人或该 UID 的授权人 | 查看该 UID 的待抢任务列表，无需请求 App 接口 |
| `/help`、`/start` | 私聊用户 | 帮助、命令格式与 UA 示例 |

`standardUA` 是 JSON 对象，可包含空格或换行；不要添加外层单引号。Bot 从 JSON 的 `jPushId` 和 `version` 字段读取推送标识和版本号，无需额外参数。`version` 必填，未注册的 `jPushId` 可省略。完整示例及可直接交给 AI 的说明见 [UA_FORMAT_FOR_AI.md](docs/UA_FORMAT_FOR_AI.md)。

会话导入消息会尝试删除，token 不回显；数据库和任务目录限制为服务用户访问。会话返回 10007 时停止并提示更新 token。

账号密码登录的消息也会尝试删除，密码和密码密文不写入数据库、日志或回复。密码包含空格时使用 JSON 双引号：`/login ACCOUNT "Pass word!"`。登录失败或个人主页返回的 UID 不一致时，不替换已保存的账号，不新增 UID 控制权限。没有登录授权的用户仍可使用已获准 UID 的 `/me`、`/list`、`/add`、`/cancel`。

模拟 UA 使用固定的 OnePlus PHK110 / Android 16 参数模板。每个账号独立保存安装 UUID 和创建时间，后续登录复用，启动时间更新；账号索引保存为摘要。`jPushId` 来自真实极光注册，模拟配置不伪造该字段。该配置不是物理设备证明，不保证服务端所有接口接受。数据库中的 `login_devices`、`login_grants` 在启动时增量创建。

每个 UID/活动仅允许一个活动中的任务。任务在开放前约 90 秒启动隔离的报名进程与 Chromium；同一进程复用浏览器缓存，最后预热 SDK，并在开放前约 12 秒预热只读连接，预热预算保留至少 3 秒余量。非必填项和备注统一为空。返回成功、失败、取消或待核对状态时通知任务创建者；授权用户创建的任务同时通知主人。默认最多并行 4 个任务，可用 `DM_MAX_ACTIVE_JOBS` 调整。

验证码直接复用当前页面已完成的背景图和滑块图响应，正常情况下只下载两张图；浏览器无法返回图片内容时才启用有超时限制的下载回退。图像库和图像处理内核在浏览器启动时预热。页面加载、图片、定位、拖动、验证回包和总耗时写入任务日志的 `captcha_timing` 事件，另记录图片复用与回退次数；日志不包含 validate。

取消在提交前生效；已经发出的请求继续核对结果，不撤销已报名状态。进程崩溃或服务重启后，已经可能提交的任务标记为待核对，不自动再次提交。首次部署会跳过部署前积压的命令，避免执行旧抢报消息。

部署目录：

- 程序：`/opt/dm-tg-bot/current`，独立虚拟环境 `/opt/dm-tg-bot/venv`。
- 密钥：`/etc/dm-tg-bot.env`，root 可读，包含 `TG_BOT_TOKEN`、`TG_OWNER_ID`。
- 数据：`/var/lib/dm-tg-bot/bot.sqlite3`；账号、权限、任务与消息游标持久化。
- 任务日志：`/var/lib/dm-tg-bot/jobs/<完整任务ID>/`，包含事件、耗时、错误、浏览器 trace 和最终结果；不记录账号 token 或完整报名参数。
- 服务日志：`journalctl -u dm-tg-bot.service`。

服务以独立用户 `dm-bot` 运行，开机自启；只在启动脚本中安装独立虚拟环境依赖，不修改其他服务。ARM64 Debian 使用 `/usr/bin/chromium`，通过 `DM_BROWSER_EXECUTABLE` 选择已有浏览器。

Bot 采用 Telegram [官方 Bot API](https://core.telegram.org/bots/api) 长轮询与私聊消息，无需公网 webhook 端口。Telegram 要求用户先打开机器人发 `/start`，机器人才能主动向其发送结果通知。
