# 第三方依赖与资产

Python 和浏览器依赖通过对应包管理器安装，不随本仓库复制二进制。

易盾 SDK 页面由安装工具从官方 Maven Central 包获取：

- 坐标：`io.github.yidun:captcha:3.6.5`
- 页面：`assets/mobile.v2.29.2.html`
- 发布包：<https://repo.maven.apache.org/maven2/io/github/yidun/captcha/3.6.5/captcha-3.6.5.aar>
- 官方项目：<https://github.com/yidun/captcha-android-demo>

下载时验证发布包与页面 SHA-256。生成页面被 Git 忽略，使用及许可依官方发布内容。测试协议向量使用人工构造的参数，未包含真实用户请求。
