# 脱敏与本机数据

仓库只纳入源码、公开协议参数、通用配置结构和合成测试向量。

不纳入：真实手机号、账户密码、会话 token/uid/jPushId、设备 UUID/序列号、Bot Token、主人 Telegram ID、服务器 IP/SSH 密码、个人邮箱、个人文件路径、运行数据库、报名记录、AES 会话密钥、验证码 validate、浏览器 trace、抓包、原始 APK/DEX/SO、研究日志和部署记录。

真实值由环境变量、交互输入或被 Git 忽略的本机文件提供。`examples/` 的凭据和标识符均为占位值；测试的 UID、TGID、token、AES KEY 和口令均为人工合成样例。

密码登录只在内存中处理密码及其请求密文；只保存会话和设备配置。`login_devices` 的账号索引使用摘要，模拟安装标识不进入 Git。`login_grants` 保存主人授予的登录权限；登录验证成功后才保存账号并授予对应 UID 控制权。

`.gitleaks.toml` 继承完整默认规则，仅按指定测试文件和精确值放行两个合成签名结果及一个 `password123` 的 JCE 测试密文；未整体排除测试或源码目录。

以下是运行协议所需的公共参数，不是用户凭据：RSA 公钥及指数、静态 IV、旧版密码变换常量、公开的业务验证码 ID、TLS 密码套件/扩展参数。TLS 测试结构中的 client_random 已清零，不携带原始握手随机数。

`network_profiles/` 中的设备型号、系统和浏览器版本是示例环境，未包含唯一设备标识；按自己的环境配置即可。原始采集文件未提交。

提交前建议再次检查待提交文件及历史：

```bash
git diff --cached --stat
gitleaks git --pre-commit --staged --redact .
gitleaks git --redact .
```

`.gitignore` 防止本机生成的账户配置、日志、数据库、密钥文件和第三方发布包被普通 `git add` 纳入。
