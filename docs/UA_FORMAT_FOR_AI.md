# 给 AI 的 standardUA 格式说明

这是到梦空间原生 API 的 `standardUA` JSON 请求头，不是浏览器的 `Mozilla/5.0 ...` User-Agent。

可直接复制给 AI 的提示词：

> 请将我提供的到梦空间手机设备信息整理成一个单行、合法的 JSON 对象，作为 standardUA。保留字段名的大小写，时间戳使用毫秒整数，不把整个对象再次编码成字符串。JSON 中包含 jPushId 和 version，Bot 直接从这两个字段读取推送标识和版本号。operator 如来自抓包则保留原有百分号编码。此处不需要 token 和 uid，它们是独立的命令参数。

结构示例（以下是占位数据，提交前替换为手机真实值）：

```json
{
  "channelName": "dmkj_Android",
  "countryCode": "CN",
  "createTime": 1700000000000,
  "device": "OnePlus PHK110",
  "hardware": "qcom",
  "jPushId": "YOUR_JPUSH_ID",
  "modifyTime": 1700001000000,
  "operator": "%E4%B8%AD%E5%9B%BD%E7%A7%BB%E5%8A%A8",
  "screenResolution": "1240-2633",
  "startTime": 1700001000000,
  "sysVersion": "Android 36 16",
  "system": "android",
  "uuid": "00000000000000000000000000000000",
  "version": "5.0.0"
}
```

Bot 私聊中的实际命令写法：

```text
/login 123456 YOUR_TOKEN {"channelName":"dmkj_Android","countryCode":"CN","createTime":1700000000000,"device":"OnePlus PHK110","hardware":"qcom","jPushId":"YOUR_JPUSH_ID","modifyTime":1700001000000,"operator":"%E4%B8%AD%E5%9B%BD%E7%A7%BB%E5%8A%A8","screenResolution":"1240-2633","startTime":1700001000000,"sysVersion":"Android 36 16","system":"android","uuid":"00000000000000000000000000000000","version":"5.0.0"}
```

JSON 内可以包含空格，也可直接粘贴多行 JSON；不要在对象外加 shell 单引号。JSON 后不再追加参数。Bot 导入时查询个人主页，校验返回 userId 与传入 UID 一致，成功后才替换保存值。
