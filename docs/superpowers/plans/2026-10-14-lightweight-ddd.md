# Lightweight DDD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 已按用户后续指令采用 Native 顺序执行，并补充技术报告第 17 节。

**Goal:** 集中 Sidecar 的任务生命周期规则，移除控制入口对完整 Agent 的构造依赖，保持现有执行与恢复行为。

**Architecture:** lifecycle.py 提供标准库纯规则，execution_control.py 提供依赖窄存储接口的控制服务。Harness、JobQueue、Store、worker 调用这些规则，但保留现有数据库事务、锁顺序与执行编排。

**Tech Stack:** Python 3.11+、Pydantic、SQLite、LangGraph、pytest、Ruff；不新增依赖。

**Spec:** [已确认设计](../specs/2026-10-14-lightweight-ddd-design.md)

## Global Constraints

- 不改数据库 schema、任务标识、事件名称、对外响应字段和状态字符串。
- 保留 Harness、JobQueue、Store 的现有调用入口；需要迁移 TERMINAL 引用时在原模块保留兼容导出。
- 保留执行锁和现有锁顺序；对账或控制服务抽取不得使其他任务提前领取环境。
- 历史数据库能够直接打开并恢复；新增领域对象不进入已保存 JSON，不要求数据迁移。
- 不引入微服务、CQRS、事件溯源、通用 Repository 框架或新依赖，不按外部 App 划分领域，也不全面重排目录。
- 先红后绿；每个任务通过其针对性测试后再提交，不混入用户已有改动。

## Review Focus

以下补充现有测试未单独固定的边界，分别纳入任务 1–3：

1. paused + 待处理 cancel 的恢复请求必须被拒绝，取消信箱保持不变。
2. 纯规则计算不可修改传入的历史状态或验证证据。
3. 无执行索引的排队任务暂停/取消不得创建图检查点或设备。
4. 清空队列遇到旧任务已完成，仍须继续取消其他未结束任务并关闭存储连接。
5. 重复启动对账不应改变已稳定的状态、再次发出设备动作或让已取消任务复活。

## 文件与接口

新增 `src/sidecar/lifecycle.py`、`src/sidecar/execution_control.py`，新增对应 `tests/test_lifecycle.py`、`tests/test_execution_control.py`。修改 `harness.py`、`jobs.py`、`store.py`、`worker.py` 的生命周期调用点及相应测试；README 补充模块职责。不搬迁 contracts.py。

## 已完成基线检查

- `.venv/bin/python -m pytest -q tests/test_harness.py tests/test_jobs.py tests/test_store.py`：83 passed，2.13 秒。
- `.venv/bin/ruff check src tests`：通过。
- `.venv/bin/ruff format --check src tests`：10 个文件存在既有差异：android.py、harness.py、jobs.py、openai_compat.py、store.py，以及 test_android.py、test_browser.py、test_jobs.py、test_server.py、test_store.py。实施只格式化实际修改文件，最终报告剩余基线差异。

### Task 1: 生命周期纯规则与事务内接入

**Files:** 新增 lifecycle.py、test_lifecycle.py；修改 harness.py、jobs.py、store.py、test_jobs.py。

**Interfaces:** lifecycle.py 输出以下标准库接口。结果只携带必要字段，不复制执行状态：

- `TERMINAL: frozenset[str]`、`RESUMABLE: frozenset[str]`；值分别为 completed/failed/cancelled 与 paused/waiting_user/interrupted。
- `StatusChange(status: str, reason: str)`：frozen dataclass。
- `control_signal(current: str, requested: str) -> str`：已接受 cancel 始终保持；信号合法性仍由现有入口校验。
- `queue_control_status(status: str, signal: str) -> str`：executing 保持原值，其他非终态按信号转 paused/cancelled；终态拒绝。
- `queue_finish(current: str, signal: str, proposed: str, reason: str) -> StatusChange | None`：既有终态返回 None；取消优先，保留现有中文取消原因。
- `execution_save(saved_status: str, signal: str, proposed: str, reason: str) -> StatusChange`：已取消或待取消覆盖提案，原因沿用 `Cancelled by user`，其他情况返回提案。
- `can_resume_queue(status: str, signal: str) -> bool`、`valid_resume_note(note: str) -> bool`：恢复状态合法且未取消，备注 strip 非空、原字符串长度不超过 1000。
- `boundary_change(signal: str, expired: bool) -> StatusChange | None`：取消、暂停、超时依次优先；文案沿用 Harness._boundary。

- [x] 写 test_lifecycle.py：参数化覆盖三种终态、三种可恢复状态、取消/暂停/空信号和预算同时发生。关键断言：`queue_finish('completed', 'cancel', 'failed', 'late') is None`；`execution_save('completed', 'cancel', 'completed', 'verified').status == 'cancelled'`；`not can_resume_queue('paused', 'cancel')`；空白备注、1001 字备注拒绝、1000 字备注通过。
- [x] 运行 `.venv/bin/python -m pytest -q tests/test_lifecycle.py`，确认因新模块缺失而失败。
- [x] 实现上述接口；在 Harness._boundary/resume、Store.save、JobQueue.control/finish/resume 使用对应规则。Harness 终态检查引用统一集合；保留索引和检查点各自的检查次序、设备匹配、清除旧动作与重新观察。Store.save 仍在 BEGIN IMMEDIATE 内获取最新信号与计算决策。JobQueue.resume 在同一事务读取 status 和 control，遇到 cancel 沿用“当前任务不能恢复”。
- [x] 将 TERMINAL 的直接依赖改为 lifecycle；Harness 保留兼容导出。领取和阻塞 SQL 保留原子条件，不把数据库查询转换成先查后写的 Python 判断。SQL 中状态字面量可保留，但用行为测试固定其与规则的一致性。
- [x] 补充 `test_resume_never_clears_pending_cancel`：构造 paused + cancel 队列行，resume 抛 ValueError，raw.control 仍为 cancel；以及 Store.save 输入含 verification 时，返回值被取消覆盖但输入字典与证据原值不变。先单独运行确认失败，再实现必要的最小修复。
- [x] 运行 `.venv/bin/python -m pytest -q tests/test_lifecycle.py tests/test_harness.py tests/test_jobs.py tests/test_store.py`，要求全部通过，包括原有完成/取消并发与跨库窗口测试。
- [x] 审阅差异并提交 `refactor: centralize task lifecycle policies`。

### Task 2: 抽取执行控制服务

**Files:** 新增 execution_control.py、test_execution_control.py；修改 harness.py、jobs.py、test_jobs.py。

**Interfaces:** 消费 lifecycle.TERMINAL；输出 `ExecutionControl(store: ControlStore)` 与 `request(task_id: str, signal: str) -> None`。`ControlStore(Protocol)` 仅定义 `get(task_id) -> dict`、`control(task_id, value) -> None`、`save(state) -> dict`、`execution_lock() -> ContextManager[None]`、`event(task_id, kind, payload) -> None`。连接生命周期由调用者管理，Store 无需继承接口。

- [x] 写服务测试，使用临时 SQLite Store：暂停仅登记请求；paused/waiting_user 取消确认终态；无人持锁的 running 取消释放环境；有执行者持锁时只记录取消等待边界；终态拒绝且不新增 control 事件。关键断言与 Harness.control 原行为一致。
- [x] 运行 `.venv/bin/python -m pytest -q tests/test_execution_control.py`，确认因服务缺失而失败。
- [x] 将 Harness.control 的编排迁入 ExecutionControl.request，保留原异常类型、事件名、锁范围和取消覆盖规则；Harness.control 委托该服务。
- [x] 先添加队列依赖边界测试：用会抛 AssertionError 的 Harness.__init__、OfflineDevice.__init__、OfflinePolicy.__init__ 替身验证 q.control/q.clear；准备执行状态后才替换构造器。断言操作成功，空执行索引时不产生 checkpoints.sqlite。运行测试确认旧代码失败。
- [x] JobQueue.control/clear 改用 Store + ExecutionControl，try/finally 关闭连接；仅在当前代码允许的已结束竞争情形捕获 ValueError。删除 jobs.py 的 Harness、OfflineDevice、OfflinePolicy 导入，保留 PREFIX。批量清空仍在现有队列事务内先转交信号，再解除队列占用。
- [x] 补充清空测试：同时存在已完成引擎和其他未结束任务，clear 后后者取消、历史保留，后续 Store 可写入；确认依赖边界测试与原批量取消测试通过。
- [x] 运行 `.venv/bin/python -m pytest -q tests/test_execution_control.py tests/test_harness.py tests/test_jobs.py tests/test_store.py`，要求全部通过。
- [x] 审阅差异并提交 `refactor: decouple task control from agent construction`。

### Task 3: 对账决策与完整兼容验证

**Files:** 修改 lifecycle.py、worker.py、test_lifecycle.py、test_jobs.py、README.md。

**Interfaces:** 新增 `recovery_action(queue_status: str, signal: str, execution_status: str | None) -> Literal['finish_from_engine', 'settle_engine', 'cancel', 'interrupt', 'keep']`。仅返回决策；worker 保留现有中文原因、连接和日志操作。

- [x] 添加参数化纯规则测试，按现有优先级断言：引擎终态且队列非终态 → finish_from_engine；两边均终态 → keep；队列终态而引擎非终态 → settle_engine；未处理 cancel → cancel；遗留 executing → interrupt；paused/waiting_user → keep。无引擎索引且队列终态 → keep。
- [x] 运行新增测试，确认缺少 recovery_action 而失败。
- [x] 实现函数，并替换 worker.recover 的分支决策；finish_from_engine 仍经 JobQueue.finish 应用已接受取消。_settle_engine 不覆盖已存在的引擎终态。
- [x] 添加 `test_recovery_is_repeatable_without_actions`：构造 executing/paused/waiting_user/已取消队列与不同执行状态组合，对账两次后状态一致；暂停交接保持现场；设备执行替身一旦调用即失败。复用已有历史 JSON fixture，不加入新字段要求。
- [x] 运行 `.venv/bin/python -m pytest -q tests/test_lifecycle.py tests/test_jobs.py tests/test_harness.py tests/test_store.py tests/test_cli.py tests/test_server.py tests/test_openai_compat.py`，要求全部通过。
- [x] README 模块表与架构说明补充纯规则、控制服务、队列/索引/检查点的权威范围，并说明保留事务保护，不把持久化检查点当作动作重放授权。
- [x] 对实际修改 Python 文件执行 Ruff format，运行 `.venv/bin/ruff check src tests`、修改文件的 format --check 和 `git diff --check`，均应通过。全仓格式检查单独报告剩余基线差异。
- [x] 运行离线全套：`.venv/bin/python -m pytest -q --ignore=tests/test_app_lifecycle_live.py --ignore=tests/test_hidden_input_live.py --ignore=tests/test_textbridge_verification_live.py`。明确报告不运行这三个真实设备验收文件；其他 skip 按实际输出记录。
- [x] 审阅完整差异，检查取消竞争、恢复不重放、旧数据库兼容与锁顺序，修复发现的问题并运行相关回归；提交 `refactor: isolate recovery decisions and document lifecycle boundaries`。

## 交付

完成时提供实际变更、测试结果和剩余限制，勾选本计划步骤并更新设计实施状态。测试未通过前不称重构完成。不启动真实设备、不重启用户运行中的服务，不推送或合并分支。


## 实施完成记录（2026-10-14）

- 分支：`codex/lightweight-ddd`；实现提交：40a25b4、e3f523d、86b0b66。
- 核心变化：新增 lifecycle 与 ExecutionControl，队列控制移除 Agent 构造，恢复决策独立，修复 paused + pending cancel 被恢复清空的问题。
- 代码不新增依赖、不修改数据库结构。技术报告第 17 节和 README 已同步。
- 完整离线回归：412 passed；标准入口：412 passed、3 skipped（真实设备验收未启用）。
- 独立审查：approve，无新增可操作问题、无暂缓项；审查者另行运行 164 项重点测试通过。
- 静态检查与 11 个修改文件格式检查通过；四个未修改文件保留既有格式差异。
- 实施取舍：保留 Store 的 SQL CASE 取消保护，纯规则在队列事务内使用，避免并发取消丢失；OpenAI 适配层也共享 TERMINAL，避免展示状态漂移，其 90 项回归通过。
- 依计划保留分支与隔离工作区，未推送、未合并、未重启演示服务。
