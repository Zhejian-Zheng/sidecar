# 副屏定向文字输入

`src/sidecar/assets/textbridge.jar` 是本目录 Java 源码编译的 DEX 容器，随 Python 包分发；运行时不需要 Java 或 Gradle。它通过 ADB 的标准输入接收单次 JSON 请求，不将文字放入命令行、文件、剪贴板或日志。桥接没有网络监听端口，不安装常驻输入法/无障碍 App。

当前支持 Android 15 隐藏副屏上的标准可编辑控件。使用 UiAutomation 的多显示屏窗口枚举和 ACTION_SET_TEXT；UiAutomation 的进程初始化使用隐藏 API，因此系统升级必须重新验收。不会自动回退到主屏、当前焦点、键盘或剪贴板。其他自动化工具占用 UiAutomation 时会拒绝连接，不终止对方。

选择器包含控件原文字，写入成功后旧选择器失效。匹配不唯一、字段变化、密码输入框、不支持设置文字、显示身份变化等情况均停止。结果不确定时先读取核对，不能直接重放。现有 uiautomator2 的 XML 观察可能把部分 emoji 转为点号，这类字段可能无法按原文匹配；不对其放宽匹配。

## 重建

需要 JDK 17、Android SDK `platforms;android-35` 与 `build-tools;35.0.0`，设置 `ANDROID_HOME` 后在仓库根目录运行：

```sh
python android/textbridge/build.py
```

源码和生成的 JAR 应一起更新。运行项目全量 pytest 和下述真实模拟器验收后再分发。

## 可重复的模拟器验收

此验收暂时在主屏打开专用测试 App。仅在可用于测试的 Android 15 模拟器运行；它不访问用户账号或网络。测试创建并清理自己的隐藏副屏，不改现有 Agent 副屏或任务。APK 使用临时测试签名，重新编译后先卸载旧测试 APK。

```sh
python android/input-fixture/build.py
adb -s emulator-5554 install .sidecar/acceptance/input-fixture.apk
adb -s emulator-5554 shell am start --display 0 -n dev.sidecar.inputfixture/.PrimaryActivity
SIDECAR_INPUT_TEST_SERIAL=emulator-5554 pytest -q tests/test_hidden_input_live.py
adb -s emulator-5554 uninstall dev.sidecar.inputfixture
```

覆盖：中英文写入、两屏同名控件隔离、原文字过期拒绝、真实设备 RPC 链路、主屏同时打字和焦点保持。普通 pytest 默认跳过这一设备验收。重复运行前关闭并重新打开测试 App，将主屏文本恢复为初始值。
