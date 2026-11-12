# Android 手机配置池

`android.json` 从 AuroraOSS/gplayapi 的公开设备配置提取必要字段，固定上游提交 `2f03e8babee8adf791d4f3a3b17c268a704be76c`。共 16 个机型、10 个品牌，筛选 ARM64、Android API 29 及以上的手机；不包含电视、平板、折叠屏或桌面配置。

保留同一条配置的制造商、型号、`Build.HARDWARE`、Android 版本/API、`Screen.Width/Height`，整组转换为 `standardUA`。屏幕数据沿用配置的上报尺寸，不等同于对每个型号面板原生像素的验证。未填充上游未提供的信息。

仅新建的账号密码登录设备使用配置池。优先分配未使用机型，全部使用后选择使用次数最少的机型；同次数时随机选择。分配在 SQLite 写事务内完成，已保存的安装配置按账号复用。token 导入保留传入的完整 UA；已有的账密设备配置保留，登录时只更新启动时间及必要的 App 版本。已保存的 token 设备也计入机型使用量，但不会被修改；同一安装不会因出现在两张表中重复计数。

每条记录包含原始配置路径及 SHA-256。可从仓库根目录重新生成：

```bash
python tools/update_device_profiles.py
```

运行 Bot 无需联网下载设备库，也无需安装 Kotlin/JVM。UUID 由本程序独立生成；配置数据不包含账户会话、物理设备唯一标识或 JPush 注册。

数据来源：[AuroraOSS/gplayapi](https://gitlab.com/AuroraOSS/gplayapi/-/tree/2f03e8babee8adf791d4f3a3b17c268a704be76c/lib/src/main/resources/com/aurora/gplayapi/device)。Copyright 2020 AuroraOSS；数据按 GPL-3.0-or-later 保留，许可全文见 [COPYING](COPYING)。本目录的数据经过字段提取和筛选，修改日期为 2026-11-12。
