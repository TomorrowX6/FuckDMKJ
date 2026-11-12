# 官方验证码页面

`mobile.v2.29.2.html` 来自易盾官方 Maven Central 发布包 `io.github.yidun:captcha:3.6.5` 的 `assets/` 目录，与当前 App 的 Android SDK 版本和页面名称对应。

- 官方项目：[yidun/captcha-android-demo](https://github.com/yidun/captcha-android-demo)
- 发布包：[captcha-3.6.5.aar](https://repo.maven.apache.org/maven2/io/github/yidun/captcha/3.6.5/captcha-3.6.5.aar)
- 发布包 SHA-256：`028370743fd1304e98fb3f092d6554387531d93afe2c3d34cec6572d07304ade`

页面保持官方内容，通过本机临时回环 HTTP 服务加载。`dm_captcha.py` 在页面初始化前接收官方 `JSInterface.onValidate` 回调，并对拼图进行定位和触摸拖动。页面相关许可遵循官方发布包及项目说明。

Run `python tools/fetch_captcha_sdk.py` from the repository root to fetch and verify this asset. The generated HTML and downloaded packages are excluded from Git.
