# 第 20 页：Agent Loop 负责生产事件

来源：`../materials/1-2-Streaming 流式输出.pdf`，第 20 / 46 页，5.4。

本页目标：能区分模型一轮调用结束与整个 Run 完成，能指出事件由谁产生、由谁保存、由谁传输。

## 核心分工

课件核心原则：Loop 负责生产事实，HTTP 层负责传输事实。

| 参与者 | 负责什么 |
| --- | --- |
| Adapter | 将平台请求/响应转换为统一模型协议 |
| Agent Loop | 调用模型、执行工具、判断下一步并产生业务事件 |
| Run Store | 保存运行状态、事件及安全 checkpoint |
| HTTP/SSE | 向客户端传输事件，支持订阅与回放 |
| Web/CLI | 按顺序消费事件、去重、展示内容与状态 |

## 贯穿例子

用户输入：“用工具计算 12 + 30，再解释结果。”下面只列关键事件，省略用量、快照等辅助事件。

```text
task.started
    ↓
模型要求调用 add
model.finished（tool_calls）
    ↓
tool.started
tool.completed（结果 42）
    ↓
Loop 将工具结果加入消息历史，再调用模型
text.delta（解释文字）
model.finished（stop）
    ↓
Loop 判断任务正常结束，写入 run.completed
```

课件的 `task.started` / `text.delta` 对应本项目的 `run.started` / `model.delta`，含义相近，字面名字不同。

## 三个边界

1. **模型事件不等于 Run 事件。**模型输出是底层数据，Adapter / Loop 转换为统一事件；工具执行与 Run 生命周期由程序管理。
2. **流结束不等于任务成功。**可能是模型要求工具、输出截断、网络中断或用户取消；正常结束还需要协议与业务判断。
3. **取消不是普通异常。**Python 的 `CancelledError` 是控制信号，须单独处理、清理资源，并尊重已有终态，不能统一改成失败。

## 对照代码

- `gateway/model.py`：`StreamingModel`、`ModelRequest`、`ModelChunk`。
- `gateway/adapters/aliyun.py`：`build_payload()`、`_decode()`。
- `gateway/agent.py`：`_loop()` 将模型增量转成事件，调用工具，保存完整步骤 checkpoint；`cancel()` / `_execute()` 处理取消。
- `gateway/store.py`：给事件分配运行内单调 seq，事务保存状态和快照。
- `gateway/api.py`：创建、订阅、取消与恢复接口，SSE 传输不承担 Agent 工具决策。

## 当前理解检查

第一次出现 `model.finished` 时，为什么不能立刻写入 `run.completed`？Loop 接下来还需要完成哪些事情？

状态：已提问，尚未收到用户回答。下一次教学先等待这个回答，再反馈并进入第 21 页。
