# 工具清单

## 本地任务队列（模板必备）

- 入口：[shell.py](shell.py)；旧 [task_queue.py](task_queue.py) 兼容入口执行相同检查。
- 规则与格式：[queue_model.py](queue_model.py)（旧协议重放）、[queue_v2.py](queue_v2.py)（五态与窗口）；状态包：[state_pack.py](state_pack.py)。
- 用法：[queue-usage.md](queue-usage.md)。
- 依赖：Python 3.10+、Git；无第三方包、网络或后台服务。
- 写入：初始化接入本机 Git 钩子；维护机器账、任务视图和本机恢复数据。不会自行修改对象产物、提交或推送。
- 验证：[队列回归](../gate/test_task_queue.py)和[状态包与窗口回归](../gate/test_state_queue.py)。

## 项目专用工具

尚未登记。确实需要时写明用途、位置、来源与版本、调用方式、前置条件、副作用及授权要求，不把本次执行结果写在此处。
