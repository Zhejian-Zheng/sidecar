# Connected Agent Implementation Plan

> 使用 executing-plans 在当前会话实施，最后独立审查。用户已授权继续开发；不提交或推送现有混合工作区。

**Goal:** 让已有 Harness 接入 Android，并提供与浏览器生命周期无关的任务控制入口。
**Architecture:** Device / Model 接口保留；独立队列 worker 调用 Harness，FastAPI 仅提交与查询；原生响应式网页消费受保护 API。
**Tech Stack:** Python、LangGraph、uiautomator2、FastAPI、SQLite。
**Spec:** `docs/superpowers/specs/2026-10-10-connected-agent.md`

## Constraints

真实 App 和模型未准备，不执行账号写入或线上模型调用。默认 loopback，API 令牌鉴权。暂停交接保留执行权。无页面关闭后推送承诺。继续开发授权覆盖本地实现，保留现有未提交内容。

## Review focus

取消崩溃任务能够释放队列；并发 worker 不重复执行；重启不得自动重放动作；未认证请求不能取得截图；设备截图变化不能静默改变点击对象。

## Task 1: Core reliability

Files: contracts.py / harness.py / store.py / model.py; tests/test_harness.py.
Interfaces: Action(type, target=None) 支持聚焦输入；Device 可提供 is_current(action,before,after)；Harness 控制在无执行者时可终结遗留任务。
- [ ] 先写并运行崩溃取消、瞬时模型超时、重试期间取消、聚焦输入、稳定目标与变化背景的失败测试。
- [ ] 实现取消持锁检查、最多两次只读模型调用、适配器目标有效性检查。
- [ ] 全部既有测试及新增测试通过。

## Task 2: Android adapter

Files: src/sidecar/android.py; tests/test_android.py; CLI observe/android。
Interfaces: AndroidDevice(serial,package), observe/assess/execute/is_current；固定 serial，观察解析 XML 元素、当前包、截图，Unicode set_text / send_keys。
- [ ] 写外部设备响应夹具，验证越界坐标、包白名单、密码界面、控件过期、中文输入。
- [ ] 实现有超时边界的库适配，目标级新鲜度判断，截图通过本地文件及 API 展示。
- [ ] 全套测试；真实模拟器只观察，记录是否可用。

## Task 3: Queue and phone console

Files: jobs.py / worker.py / server.py / static/*; tests/test_jobs.py / test_server.py。
Interfaces: JobQueue.submit/list/get/control/resume，worker.run_next(home,config)；GET/POST /api/tasks、/api/tasks/{id}/{pause,cancel,resume}、/api/tasks/{id}/frame；GET /api/config。
- [ ] 先测试未经授权、重复提交键、任务跨进程完成、队列取消、暂停保留执行权、worker 重启标记交接。
- [ ] 实现持久队列与独立 worker；服务读写短连接 SQLite，不在请求内执行 Agent；错误返回可理解信息。
- [ ] 实现手机页面：配对、任务输入、列表与状态、最新截图、证据、暂停/取消/恢复；离线标签与通知限制清晰展示。
- [ ] 验证浏览器请求结束后任务完成；真实浏览器桌面与窄屏检查。

## Task 4: Documentation and review

- [ ] README 添加设备、服务、配对、独立 worker、启动/停止与限制说明。
- [ ] 全部测试、lint、format、依赖安装、CLI smoke。
- [ ] 独立代码审查，实质问题以回归测试修复，记录验证范围与未完成前置条件。

## Ledger

- Ruling: 沿用用户当前工作区，新建 codex/android-control 分支；不创建 worktree，以保留未提交核心实现。
- Ruling: 执行授权来自本轮“继续完成”；沿用先前原生实现方式，不再逐阶段请求批准。
- Pre-flight: 所有模块以已有 Task/Observation/Action 为边界；队列 ID 与 Harness task_id 一致，配置信息不含密钥。
