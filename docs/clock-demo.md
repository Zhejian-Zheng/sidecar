# Google 时钟演示：运行、核对与录屏

本次演示范围：自然语言任务通过模型决策，在 Android 模拟器的隐藏副屏操作真实 Google 时钟；主屏继续用于其他 App。闹钟保存在该 Android 实例中，不会同步为 iPhone 系统闹钟。不使用固定点击脚本或业务 API 创建闹钟。

## 已完成的实测（2026-10-14）

- 真实模型任务：设置并开启 23:58 闹钟，已有同时间闹钟则复用，保留其他闹钟。
- 任务 `e603a87660994658b82c3daad1479fa4`，结果 `completed`。
- 执行事件起止为北京时间 10:56:45–10:58:01，约 76 秒、7 次模型决策。
- 最终验证观察 `ea54524bd0434014ad56f96f56c7cf0f`，截图显示 11:58 PM 和开启的开关。
- 关闭原副屏后，在新的临时副屏重开时钟核对：23:58 仍开启，原有 8:00 闹钟保留；未再次创建或修改闹钟。独立核对截图为 `.sidecar/clock-demo-20261014/reopened.png`。
- 主屏前台为 Notion；26 次采样均未变化。这是前台未切换的采样证据，不是用户持续操作或输入法完全不受影响的证明。
- 本机证据位于 `.sidecar/clock-demo-20261014/evidence/`，原始运行数据位于 `.sidecar/clock-demo-20261014/run-1791946602/`。这些目录不会提交 Git；分享前核对截图、目标和记录中的私人信息。

这是一条成功样本，不能据此推算稳定成功率。最终 1–2 分钟同期演示视频仍需录制，并展示用户实际持续操作。

## 演示前准备

1. 启动 Android 15 或以上模拟器，安装 Google 时钟（`com.google.android.deskclock`）。主屏先打开其他 App；不要让时钟占用主屏。
2. 使用项目虚拟环境，确保 `adb`、`scrcpy`、`ffmpeg` 在 PATH。本机工具路径如下；其他机器按实际安装位置调整。
3. 在启动终端配置 `SIDECAR_MODEL`、`SIDECAR_BASE_URL`、`SIDECAR_API_KEY`，见 [模型配置说明](deepseek.md)。程序不会自动读取 `.env`，不要输出或录制密钥。
4. 使用独立运行目录和空闲端口。若旧演示有暂停任务，先核对并取消；不要将旧副屏任务恢复到新的副屏。

```sh
export PATH="/opt/homebrew/share/android-commandlinetools/platform-tools:/opt/homebrew/bin:$PATH"
adb devices -l
adb -s emulator-5554 shell pm path com.google.android.deskclock
.venv/bin/sidecar --home .sidecar/clock-recording serve --android --hidden-display \
  --serial emulator-5554 --package com.google.android.deskclock --port 8767
```

打开 `http://127.0.0.1:8767`，用终端显示的配对码连接。开始录屏前完成配对，避免录入令牌。`--host 0.0.0.0` 可供同一可信局域网手机访问 Mac 的局域网地址；默认只监听本机。HTTP 入口只用于可信网络演示，不应直接暴露公网。

## 提交与核对

在网页输入：

> 在 Google 时钟中设置一个晚上 11:58（23:58）的闹钟并开启。如果已经存在该时间的闹钟，直接使用它，不重复创建。保留其他闹钟。

可选完成标准，每行一条：

```text
闹钟列表中存在晚上 11:58（23:58）的闹钟
该闹钟的开关为开启状态
```

23:58 是已实测示例；录制当天选一个不会在演示中立刻响铃的时间。若要展示完整创建过程，应先在时钟中人工准备一个不存在该时间闹钟的起始状态；已有闹钟时 Agent 复用它是正确行为。不要通过反复提交来试图强制重复创建。

- 提交后让用户持续在主屏滚动或输入；不要仅停留在一张静态页面。
- 点击“查看执行画面”可看最近观察截图，但它不是连续视频。
- 完成后逐项核对结果：时间、AM/PM、开启状态。worker 会关闭副屏窗口，已保存数据仍保留；网页留有最近截图。
- 若需展示暂停，等待页面明确显示“已暂停”后再人工操作，填核对备注后继续。暂停不撤回已发出的操作。
- 若失败或交接，保留真实状态和错误信息。先检查是否已经保存，再决定恢复或重新提交。

## 同期录屏

为展示实时副屏，可在服务运行时从配置读取副屏编号：

```sh
.venv/bin/python -c "from sidecar.jobs import JobQueue; print(JobQueue('.sidecar/clock-recording').settings().display.id)"
```

把打印的编号填入下面的命令；该镜像禁用输入控制、音频与剪贴板同步：

```sh
scrcpy --serial emulator-5554 --display-id=<副屏编号> \
  --no-control --no-audio --no-clipboard-autosync --window-title='Sidecar 执行画面'
```

用 macOS 录屏同时包含主屏镜像、副屏镜像和控制台。建议约 10 秒提交、约 80 秒真实执行、约 20 秒核对结果；实际任务可能更长，超过时如实说明耗时，或更换更短的任务。不要将加速、剪切录像表述为连续实时执行。当前没有离页系统推送，演示中使用控制台完成提示。

手机持续使用的证明需要用户实际操作；后台采样日志、离线测试或静态主屏录像都不能替代。

## 导出演示证据

任务结束后，从响应或 `sidecar status` 获取任务编号：

```sh
.venv/bin/sidecar --home .sidecar/clock-recording export <任务ID> \
  --output .sidecar/clock-recording-evidence
```

命令写出 `report.json`（状态、模式、目标、验证与观察编号）、`events.json`（全部已记录事件），以及有图片时的 `observation.png` / `observation.jpg`。已有目录会被拒绝覆盖。导出是某一时刻的记录，不自动判定成功，也不提供完整视频或独立正确性保证。运行中导出的数据可能继续变化，提交材料应使用任务结束后的导出。

## 提交前最后核对

- 视频有用户连续操作、Agent 真实执行和最终结果，且不出现密钥或配对码。
- 闹钟只在演示 Android 实例内生效，答辩中明确这一设备范围。
- 重启服务前结束任务；演示后可手动关闭测试闹钟，避免稍后响铃。
- 仓库提交说明与视频一致，不将模拟器结果写成 iPhone 同步或通用 App 支持。
