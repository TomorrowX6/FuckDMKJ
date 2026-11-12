# FuckDMKJ

到梦空间 CLI 与 Telegram Bot：会话导入、按 UID 授权、定时活动报名、任务取消、个人信息查询及密码登录工具。

此仓库是脱敏后的源码版本。账号会话、手机号、密码、Bot Token、主人 TGID、服务器地址、运行日志、抓包和原始 App 文件均不随源码提交。示例中的标识符和凭据全部需要替换。

## 安装

需要 Python 3.11+、Chrome 或 Chromium。

```bash
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows PowerShell：.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-grab.txt
python tools/fetch_captcha_sdk.py
```

验证码 SDK 页面从官方 Maven 包下载并核对 SHA-256；第三方发布包和生成的 HTML 不进入 Git 历史。网络配置在 `network_profiles/`，可按实际设备调整。Linux ARM64 可设置 `DM_BROWSER_EXECUTABLE=/usr/bin/chromium`。

## CLI

先把示例复制到本机私有配置目录，填写真实手机的设备头和会话：

```bash
mkdir -p requests/mine_homepage
cp examples/standardua.example.json requests/mine_homepage/standardua.json
cp examples/profile.example.json requests/mine_homepage/payload.json
```

也可以先准备设备头，再运行密码登录命令；密码隐藏输入，成功后保存会话，旧文件会备份：

```bash
python dm_login.py
python dm_grab.py ACTIVITY_ID --inspect
python dm_grab.py ACTIVITY_ID
```

ACTIVITY_ID 替换为真实活动 ID。输入式多活动模式运行 `python dm_grab.py`。完整用法见 [CLI 说明](README.dm-grab.md)。

## Telegram Bot

复制 `.env.example` 为本机 `.env`，填写自己的 Bot Token 和主人 Telegram ID，再将变量导入运行环境。Linux 示例：

```bash
set -a
. ./.env
set +a
python dm_bot.py
```

私聊中支持：

```text
/login UID TOKEN standardUA_JSON
/Permissions UID TGID
/add UID 活动ID
/cancel UID 活动ID
/me UID
/list UID
/help
```

主人负责导入会话和授权；其他用户只能控制获准 UID。`jPushId` 和版本号从 `standardUA` 中读取。`/cancel` 只停止待抢任务，不撤销成功报名。

[Bot 使用与部署](README.tg-bot.md) · [给 AI 使用的 UA 格式](docs/UA_FORMAT_FOR_AI.md) · [隐私与文件说明](docs/privacy.md)

## 测试

```bash
python -m pip install -r requirements-test.txt
python tools/fetch_captcha_sdk.py
python -m pytest -q
```

测试使用本地 HTTP/TLS 服务、合成会话、合成图片和独立协议向量，不会真实报名。浏览器测试需要已安装 Chrome，或通过 `DM_BROWSER_EXECUTABLE` 指向 Chromium。

## 插桩工具

`instrumentation/` 保存协议观察、线程和退出调用跟踪脚本。可单独安装 `frida-tools`，按需要加载脚本；生成的抓取结果只保存在本机。原始 APK、DEX、SO、设备序列号和捕获日志未包含在仓库中。
