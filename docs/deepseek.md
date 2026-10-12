# 在 Sidecar 中使用 DeepSeek

更新日期：2026-10-12。适配代码和本地 HTTP 协议测试已完成；已使用真实 DeepSeek 调用操作 Mac 上 Android 模拟器的 Google 时钟，创建并核对 21:00 闹钟。此次包含调试暂停与恢复，不代表稳定成功率或任意 App 能力。

## 1. 接入方式

继续使用现有 LangChain 模型适配器。设置官方 DeepSeek 地址后，程序自动选择其 Chat Completions JSON 模式，不需要增加另一套 Agent 或 SDK。

| 配置 | 推荐值 |
| --- | --- |
| `SIDECAR_MODEL` | `deepseek-flash` |
| `SIDECAR_BASE_URL` | `https://api.deepseek.com` |
| `SIDECAR_API_KEY` | 在你自己的 Mac 上输入的 DeepSeek 密钥 |

官方目前列出的 `deepseek-flash` 支持图片输入，适合读取 App 截图；`deepseek-v4-pro` 不支持图片。程序也识别官方列出的两个旧 Flash 别名；其他 DeepSeek 型号按文本模式处理，除非后续单独验证并补充能力声明。[模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、[图片输入说明](https://api-docs.deepseek.com/guides/vision/)

纯文本模式仍能使用界面文本和控件信息，但不发送图片，并把模型提出的坐标点击转为人工交接。这不等于完整的视觉 Agent 能力。使用第三方 DeepSeek 转发平台时，不会自动套用官方适配；需另行核对协议和模型能力。

DeepSeek 的 JSON 模式使用 `response_format: {"type": "json_object"}`。我们在请求中提供 JSON Schema 与格式示例，并在本机用 Pydantic 校验动作和验证结果。合法 JSON 不代表合法操作，无效参数会被拒绝。官方提示偶尔可能出现空响应；当前将其作为解析失败处理，不自动执行或重放任何设备动作。[JSON 输出说明](https://api-docs.deepseek.com/guides/json_mode/)

当前 DeepSeek 请求使用非思考模式、最多 4096 个输出 token、30 秒请求超时；SDK 不自动重试。Harness 只对暂时性模型连接错误做有上限的重试。规划、决策、验证入口均禁用继承自全局环境的 LangSmith 追踪。

## 2. 在 Mac 上配置密钥

在 [DeepSeek 平台](https://platform.deepseek.com/api_keys) 创建 API Key。密钥只在自己的终端输入，不需要发到聊天中。

打开 macOS 默认的 zsh 终端，进入项目并运行：

```zsh
cd /Volumes/Work/github/sidecar
source .venv/bin/activate
export SIDECAR_MODEL='deepseek-flash'
export SIDECAR_BASE_URL='https://api.deepseek.com'
read -rs 'SIDECAR_API_KEY?请粘贴 DeepSeek API Key，然后按回车：'
export SIDECAR_API_KEY
printf '\n'
```

输入密钥时终端不显示字符，是正常现象。密钥保留在当前终端会话的环境中；关闭终端后需要重新配置。不要用输出环境变量的方式检查密钥。

仓库提供 `.env.example`，但程序不自动读取 `.env`。如果以后选择保存配置，应将副本放在已被 Git 忽略的 `.env` 中，限制文件权限，并显式导入。上面的交互输入方式不需要把密钥写入文件。

## 3. 先测试模型，再连接真实 App

配置后可以手动运行：

```zsh
sidecar --home .sidecar/deepseek-demo demo --llm \
  --goal '请把检查 DeepSeek 接入加入待办，保存后检查结果' \
  --item '检查 DeepSeek 接入'
```

这条命令会产生真实模型 API 请求和相应费用，发送上述目标、模拟界面文字与短操作历史。它操作的是本地模拟清单，不会操作 Android 或真实账号；演示环境没有截图，因此成功也不能证明图片请求已经在线验证。

输出中的 `mode: llm_demo` 表示模型在控制模拟环境。预期 `status: completed`，验证记录中包含目标事项。若出现认证、余额、模型不可用或格式错误，先处理模型连接，不要直接在真实账号上反复试错。

Microsoft To Do 尚未验收。当前已用 Google 时钟（`com.google.android.deskclock`）开展真实界面联调；启动命令见 [README](../README.md#在-mac-的-android-模拟器里操作真实-app)。其他 App 仍需安装、配置包名并单独验收。

已有后台 worker 会保留启动时的环境变量；在新终端设置密钥不会修改旧进程。正式切换模型前应先结束或接管活动任务，再用新配置重启执行端。完整 worker 生命周期管理仍是待办，不应连续启动多个服务来尝试刷新密钥。

## 4. 验证范围

本次新增测试使用离线 HTTP 响应，经过真实 LangChain 与 OpenAI 客户端的请求序列化和响应解析，验证以下行为：

- 规划、决策、验证均使用 DeepSeek JSON 模式，提供输出契约并保留原始任务目标。
- Flash 发送图片；纯文本型号不发送图片，不执行无视觉依据的坐标点击。
- 其他兼容端点保留原先的 JSON Schema 模式。
- 返回越界坐标时，本地拒绝动作。
- 规划入口也关闭继承追踪。

这些离线测试验证协议适配；真实设备联调的范围和限制另列如下。

2026-10-11 后续修复验证：全量离线测试 **68 项通过、0 项失败**，静态检查通过。DeepSeek 与原有模型测试合计 **9 项通过**。此前三个失败现已通过：

- `test_sensitive_field_after_element_limit_still_blocks_screen`：控件上限后仍检查密码字段与支付文案，敏感页面不提供截图。
- `test_resume_preserves_original_plan`：恢复沿用已保存任务，不再次调用规划。
- `test_accepted_cancel_wins_during_planning`：规划异常不会覆盖已接受的取消。

另外增加了队列终态保护、取消与完成的并发测试，以及执行器与队列之间的取消对账测试。以上是 2026-10-11 的历史验证结果。

## 5. 2026-10-12 模拟器联调

- 使用真实 `deepseek-flash` API、`emulator-5554` 和 Google 时钟，通过界面点击创建了一个 9:00 PM（21:00）闹钟，开关为开启。
- 创建过程中遇到读取失败、控件编号漂移和 PM 状态误判；经调试暂停、修复、重新观察后继续。原创建任务在保存后的读取环节失败，保留失败记录，没有伪造为成功，也没有再次提交创建请求。
- 后续独立核对任务 `2fdd2d6a1d8942e1b04e31a4ecd5c435` 只折叠闹钟卡片并验证：21:00 已启用，原来的 8:30 AM 和 9:00 AM 闹钟仍存在。该任务两次决策后完成。
- OpenAI SDK 实际接收了查询的 SSE 流和最终验证结果，查询前后任务数量不变。当前服务为 Android 模式，端口 8766，worker 在线。
- 修复包括：只启动批准的 App 并检查前台；写入 RPC 不透明重发；只读采集失败最多恢复一次；控件编号不受无关节点增减影响；传递勾选状态；删除入口按区域拦截，保留读取与独立安全控件操作。

本轮全量自动化测试 137 项通过。该结果覆盖一个模拟器、一个时钟 App；没有测试登录、跨设备同步、其他第三方 App，也没有证明长期稳定运行。worker 生命周期管理仍需完善；模型可能误判界面，最终验证仍需保留。
