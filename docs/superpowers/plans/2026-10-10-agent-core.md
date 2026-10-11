# Sidecar Agent Core Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. 用户已明确要求开始开发，本轮在当前会话推进核心实现。

**Goal:** 实现可运行、可测试、可恢复的 GUI Agent 核心，保留真实设备接入边界。

**Architecture:** LangGraph 显式组织观察、决策、动作执行、人工交接与验证。SQLite 存储检查点及动作记录；设备和模型分离。

**Tech Stack:** Python 3.11+、LangGraph、LangChain 模型适配、SQLite、pytest。

**Spec:** `docs/core-design.md`

## Global Constraints

- 只实现通用核心与明确标注的离线演示，暂不连接真实设备。
- 不重试结果未知的界面动作；交接后重新观察。
- 不自动读取或使用用户已有 API 密钥；真实模型由用户显式配置。
- 保留 README 已有内容，更新实施状态，不提交运行数据。

## Review Focus

- 进程在设备动作之后、检查点之前退出：不能重复提交。
- 用户在模型调用期间请求暂停/取消：下一次动作之前必须生效。
- 模型声称完成但缺少全部条件证据：不能进入完成状态。
- 恢复时设备或任务配置不一致：不能操作错误环境。
- 多实例争用同一工作目录：不能同时控制设备。

## Task 1: Core contracts and execution

Files: `src/sidecar/contracts.py`, `store.py`, `harness.py`, `tests/test_harness.py`.

Interfaces: `Task(goal, criteria)`, `Observation`, `Action`, `Verification`; `Device.observe/execute/assess`; `DecisionModel.decide/verify`; `Harness.start/resume/control/status`.

- [x] 写测试；确认初始入口缺失和后续回归测试失败。部分初始测试在依赖安装后与实现一起运行，未逐项记录红灯。
- [x] 实现类型检查、SQLite 日志与 LangGraph 流程。
- [x] 覆盖交接、取消、重复提交、错误恢复和资源预算。
- [x] 运行核心测试并修复失败。

## Task 2: Model adapter and runnable demo

Files: `src/sidecar/model.py`, `demo.py`, `cli.py`, `tests/test_model.py`, `tests/test_cli.py`.

- [x] 编写上下文限制、结构化输出和 CLI 测试。
- [x] 实现可配置模型适配；凭据只从显式配置读取。
- [x] 实现持久化离线演示与运行、恢复、状态、事件、控制入口。
- [x] 验证离线完整执行、交接恢复与取消。

## Task 3: Documentation and final validation

- [x] 更新 README：安装、演示、模型接入、真实设备接口和当前限制。
- [x] 运行完整测试、静态检查和 CLI 演示。
- [x] 检查差异与独立代码审查，修复实质问题，记录最终结果。

## 执行与审查记录（2026-10-10）

- 完整测试：32 项通过，覆盖核心、模型适配边界及独立 CLI 进程。
- 静态检查、代码格式与 Git 空白检查通过。
- 实际运行离线 CLI：两个待办通过七次决策创建并逐项验证；该结果仅证明模拟环境的流程。
- 模型适配使用受控响应测试，未调用在线模型；真实设备、第三方 App 与跨端同步未验证。
- 已检查本地 README、实现和测试，并完成一次独立代码审查。首次审查模型因额度无法执行，替代审查完成；未把失败调用计为审查完成。

### 发现并修复

| 问题 | 修复与验证 |
| --- | --- |
| 取消可被恢复清空或被后来的暂停覆盖 | 控制请求在数据库内原子更新，取消保持最高优先级；两项回归测试先失败后通过 |
| 首次检查点之前崩溃导致恢复缺少完整状态 | 从任务索引恢复完整初始状态，重新观察；回归测试先失败后通过 |
| 交接节点可覆盖已确认取消的状态 | 状态保存使用事务保留取消结果，并传播到图节点及返回值；回归测试先失败后通过 |
| 有未结束任务时，新演示提前修改设备状态 | 将演示准备移动到取得执行锁并成功登记任务之后；回归测试先失败后通过 |

已知限制：取消无法撤销已执行动作；动作日志不提供跨设备 exactly-once；初始化崩溃恢复要求用户先核对设备与准备状态，不会自动重放准备回调；真实设备适配器需要单独实现超时、敏感操作策略和可信结果核验。
