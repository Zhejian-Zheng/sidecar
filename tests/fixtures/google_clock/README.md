# Google Clock 真实观察样本

2026-10-15 采自 Android 15 / SDK 35 模拟器的 scrcpy 隐藏副屏，320×640。
App 为 `com.google.android.deskclock` 7.5（76002074），界面英语、12 小时制。
系统 locale 返回空、time_12_24 返回 null，不能把显示格式当作已读取的系统设置。

每个目录包含 `hierarchy.xml`、`screen.png`、`metadata.json`。
XML 来自设备 UiAutomator，按绑定 display-id 过滤并重新序列化；保留原节点属性和父子关系，
不是从 Observation 重建，也不是整台设备的原始 dump。截图前后树和前台组件一致才保存。
元数据含版本、显示会话、采集时间、文件 SHA-256；标签仅是人工分类，不是验证结论。
所有截图均已人工检查，仅含测试闹钟及系统控件。

| 样本 | 实际观察 | 不能证明 |
|---|---|---|
| list-disabled | 8:00 AM、11:58 PM 两个可见记录均关闭 | 全局列表完整 |
| target-enabled | 8:00 AM 关闭，11:58 PM 开启 | 没有屏幕外重复 |
| expanded-enabled | 11:58 PM 已保存卡片展开，开关开启 | 其他闹钟被删除或保留 |
| time-picker | 时间选择器显示 11:58 PM，未点击 OK | 新时间已经保存 |
| label-dialog | 标签弹窗及输入法浮层 | 背后记录当前可操作 |
| restored-disabled | 采集后两条可见记录恢复关闭 | 所有隐藏字段完全没变 |

采集只临时开启已有 11:58 PM 闹钟；选择器、标签弹窗均取消，没有创建或删除闹钟。
恢复样本与初始样本中 onoff 的时间、开关状态一致。会话结束销毁副屏，主屏前台组件一致；
这不是用户持续操作时零干扰的性能证明。

`list-disabled` 的原始采集标签误写为 list-enabled，归档时已修正，保留 original_capture_label。
XML、PNG 未修改；空 locale/未设置时间格式在元数据中统一为 null。

## 提取器设计依据

- 非 checkable 节点也会带 checked=false；应解释为不适用，而非关闭。
- 一个记录的时间在 digital_clock 的 text、content-desc，以及 onoff、arrow 中重复。
- onoff 的 description 自带时间；必须同时校验精确包名/resource-id、checkable 和所属卡片。
- 卡片是 androidx.cardview.widget.CardView；开启后卡片描述可能只有 ` Alarm`。
- digital_clock 是可点击 TextView，不能假设它一定被现有 elements 过滤。
- 展开态属于已保存记录，可证明局部事实；不能凭此判断列表完整或其他记录状态。
- 弹窗截图中仍看得到底层卡片，但 XML 主要暴露弹窗；label-dialog 还含输入法包节点。
- 没有可依据的稳定 record ID、全局完整性标志或可靠 window ID。

缺失 checked、截断、同时间不同卡片、12 AM/PM 边界、残留旧列表的遮挡情形尚无真实样本。
以后人工构造这些负例时必须单独标为 synthetic，不能混称真实采集。
