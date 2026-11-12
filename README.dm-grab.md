# CLI 使用说明

在仓库目录运行；协议实现默认读取项目内的 `dm_cli.py`，不依赖某个用户的 Downloads 目录。

```bash
python dm_grab.py
python dm_grab.py ACTIVITY_ID
python dm_grab.py ACTIVITY_ID --inspect
python dm_grab.py ACTIVITY_ID --dry-run
```

不指定活动 ID 时可连续输入多个 ID，复用同一个浏览器会话。指定 ID 时只运行一次。程序读取报名开放时间，到点获取报名表、完成验证并提交；需要排队时核对最终结果，未知结果不会盲目重新提交。

账号配置为 `requests/mine_homepage/payload.json`，设备头为 `requests/mine_homepage/standardua.json`，两个文件均被 Git 忽略。可使用 `--profile`、`--standard-ua`、`--cli` 指向自己的文件。`examples/` 仅提供占位结构。

```bash
python dm_login.py
python dm_login.py --account YOUR_ACCOUNT --output requests/another-account/payload.json
python dm_grab.py ACTIVITY_ID --profile requests/another-account/payload.json
```

密码通过本地隐藏输入读取，不接收命令行密码参数；登录成功才更新会话，旧文件先备份。CLI 导入已有手机 token 亦可；密码重新登录可能使手机旧会话失效。

非必填项和备注统一留空。必填项在交互终端提示，也可用 `--answers answers.json` 提供 JSON，键为字段标题或 fullid。`--remark` 为旧命令兼容参数，备注仍为空。

默认使用 `curl_cffi` 的 App 网络配置，优先保持预设的 TLS1.2/HTTP1.1 行为；`--network-profile generic` 使用 HTTP2 优先模式，`--transport urllib` 使用兼容传输。证书与主机名校验保持开启。已知只读请求复用连接，报名及未知写请求使用新连接，避免隐式重发。

验证码浏览器提前预热 SDK 与图像库，复用浏览器完成的图片响应。返回最终 SDK validate，验证失败按次数上限刷新，不以原始验证接口的中间值替代。日志记录阶段耗时，真实会话和 validate 不适合作为测试样本提交。

`--dry-run` 生成的预览可能含会话、加密参数和 AES KEY，只存本机。`logs/`、`requests/`、数据库和备份均已加入 `.gitignore`。
