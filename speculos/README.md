# Speculos（Ledger 设备模拟器）

`apps/<model>/app.elf` 来自 Ledger 官方 workshop 仓库
[LedgerHQ/device-management-kit-ts-workshop](https://github.com/LedgerHQ/device-management-kit-ts-workshop/tree/main/xrpl-hackathon/speculos)
（commit 4b85faa），是 [LedgerHQ/app-xrp](https://github.com/LedgerHQ/app-xrp) 2.8.1 的构建产物，Apache-2.0 许可。

启动：`scripts/start_speculos.sh [stax|flex|apex_p|nanox|nanosp]`（默认 stax）。

- HTTP API / DMK 传输：http://127.0.0.1:5001（宿主机 5000 常被 macOS AirPlay 接收器占用，所以映射到 5001）
- APDU over TCP：127.0.0.1:9999
- VNC：vnc://127.0.0.1:41000，密码 speculos

第一次启动会生成随机 seed 写入 `speculos/.seed`（已加入 .gitignore），之后复用，设备地址保持不变。
这个 seed 只用于 testnet 演示，不要用它保管真实资产。
