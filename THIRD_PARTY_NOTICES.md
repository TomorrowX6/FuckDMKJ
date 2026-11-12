# 第三方依赖与资产

Python 和浏览器依赖通过对应包管理器安装，不随本仓库复制二进制。

易盾 SDK 页面由安装工具从官方 Maven Central 包获取：

- 坐标：`io.github.yidun:captcha:3.6.5`
- 页面：`assets/mobile.v2.29.2.html`
- 发布包：<https://repo.maven.apache.org/maven2/io/github/yidun/captcha/3.6.5/captcha-3.6.5.aar>
- 官方项目：<https://github.com/yidun/captcha-android-demo>

下载时验证发布包与页面 SHA-256。生成页面被 Git 忽略，使用及许可依官方发布内容。测试协议向量使用人工构造的参数，未包含真实用户请求。

`device_profiles/android.json` 的手机配置字段来自 [AuroraOSS/gplayapi](https://gitlab.com/AuroraOSS/gplayapi)，固定提交 `2f03e8babee8adf791d4f3a3b17c268a704be76c`。Copyright 2020 AuroraOSS，GPL-3.0-or-later；提取、筛选及来源校验信息见 [设备配置说明](device_profiles/README.md)，许可全文保存在 [device_profiles/COPYING](device_profiles/COPYING)。该数据不包含设备或用户的唯一标识。
