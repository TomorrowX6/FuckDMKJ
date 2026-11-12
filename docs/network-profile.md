# 网络配置样例

App 配置以 Android App5.0.0 的 TLS1.2/HTTP1.1 结构为兼容基准，包含 JA3、签名算法及 HTTP 请求头顺序。没有 ALPN 时不存在 HTTP2 SETTINGS。`generic` 配置保留 HTTP2 优先。

`network_profiles/dm_android16.json` 中的 ClientHello 仅是结构测试夹具，32 字节 client_random 已替换为零，未包含原始捕获。测试比较时排除随机数字段，避免把结构匹配误写成逐次随机字节一致。

这些参数不保证 TCP/IP、TLS 记录分片、会话恢复、业务时序或完整设备指纹一致。证书校验仍使用正常信任链，配置不会让失效 token 重新有效。
