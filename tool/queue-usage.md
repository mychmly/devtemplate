# 命令树与队列用法

面向代用户操作的 Agent。Python 3.10+、Git，无第三方包。模板根执行；全局 `--root <模板根>`、`--wait <秒>` 放在命令组前。返回 JSON；路径除明确绝对的 `base` 外，均相对 Git 仓库根，嵌套模板也不例外。

## 接入与恢复上下文

```text
python3 tool/shell.py init
python3 tool/shell.py doctor
python3 tool/shell.py state get
python3 tool/shell.py task status
```

先按入口读取区域契约、核对工作对象位置和用户授权，填写 truth/goals.md；需要常驻的规则写进对应区域契约，不另建接入表。新克隆须 init 安装本机钩子；已有钩子保留并串接。doctor 应为 `protection: ready` 且 `protocol: 2`。原 v1 账只读，须按下文显式升级，不自动猜测转换。

`state get` 返回 `context`、`base`、`manifest` 和 `files`。**按 files 中每个 source 的 parts 顺序读取全部分片**；拼接即该源文件全文，无附加摘要。清单是校验元数据，不是第三类业务内容。首次 Session 读全包；后续可按源指纹识别变化再读。charter/入口规则仍按 AGENTS.md 单独阅读，不被偷偷加入状态包。

正文唯一来源：配置白名单内 truth 文件全文，加全部窗口任务包文件全文。候裁池（登记）、通过、已取消档案不入包，外部引用不递归展开。分片默认 24000 字符，可用 `--chunk-chars N` 改运输尺寸，不是 token 数，不改变送达内容；不同尺寸独立保存，不破坏其他调用者的分片。

每次正常任务写入携带刚获取并读过的 `--context <版本>`。源、队列或配置变化时旧版本拒绝，重新获取并读变化，不凭空替换版本号。`state check --context <版本>` 同时检查新鲜度及缓存完整性；缺片或被改后重新 get 重建。工具不证明模型已经理解，只对受控写入检查依据版本。

## 任务命令

每个写操作带 `--actor <执行者> --request <稳定请求号> --context <状态版本>`；已有任务另带 status 返回的 `--expect <revision>`。用户决定带 `--by <用户> --basis <指令依据>`。这些是代书与归因，不是身份认证；用户已明确批准的范围不重复请批。

| 命令 | 参数与作用 |
|---|---|
| task register | `--proposal <方案文件>`；可选 `--parent`、重复 `--dep`；登记目标与计划 |
| task register --approve | 再带 `--by --basis`；同次指令可登记并批准，五阶段的登记与授权依据均保留，不要求用户重复确认 |
| task approve 编号 | `--by --basis`；冻结目标、范围、验收标准及关系，入窗 |
| task claim 编号 | 就绪且无人领取时认领，进入领取 |
| task deliver 编号 | 重复 `--artifact <Git根相对路径>`、`--receipt <文本文件>`、`--verification passed或failed或unverified`、`--summary <摘要>`；进入交付待验 |
| task pass 编号 | `--by --basis`；仅交付状态，本轮有效验证申报 passed，工件未变，用户明确通过，才封存 |
| task revise 编号 | 登记时 `--proposal --basis`；可选关系参数；省略关系保留，`--clear-parent`/`--clear-deps` 明确清空 |
| task replan 编号 | 领取时 `--plan <文本文件> --basis <原因>`；改计划，不改批准范围 |
| task rework 编号 | 交付后 `--basis <返工原因>`；认领者回到领取，旧回执保留但失去本轮通过资格，必须重新交付 |
| task handoff 编号 | `--text <说明正文>`；领取或交付的认领者留说明，不释放认领；该参数不是文件路径 |
| task release 编号 | `--text <说明正文>`；让出回到批准，清除本轮交付资格，但仍占窗口；接手后重验重交付 |
| task revoke 编号 | `--by --basis`；退回登记，释放认领和窗口名额；再次批准须重新交付 |
| task cancel 编号 | `--by --basis`；移出当前任务集合，保留取消历史；不是第六状态，不等于通过 |
| task status [编号] | 当前任务，五状态与窗口占用/就绪原因；已取消编号明确提示查历史 |
| task history [编号] | 历史事件与文件入口；取消记录没有当前 status，不会被当成完成的前置 |

revoke/cancel 的 `--tree --expect-seq <全局序号>` 只用于用户已明确授权的整组操作。默认不隐式级联批准父子。父先批准、孩子再批准；在窗且没有孩子的任务占位，有孩子的父任务在集成期也不占位，但其在窗包仍全部送达。窗口默认 8，批准时锁内核对，不截取前 8 件、不藏任务；交付仍占位，通过、撤销、取消释放占位。

方案用 queue/templates/task.md；其目标与计划由工具形成 goal.md、plan.md，task.md 保存登记、状态和过程入口。批准基线与回执随任务保存。取消的新协议任务文件移到 `.shell/queue/archive/<编号>/`，可经 history 查阅，不手动修改；编号永不复用。已通过任务原地封存，不入状态包。外部业务产物只作文件指纹引用，不自动删除或回滚。

## 配置

`charter/config.json` 是唯一配置界面，键只有 `version`、`window_capacity`（默认 8）、`truth_whitelist`（默认 truth/goals.md）。初次 init 前可按已批准范围准备；init 后由命令维护，事件账保存配置变更历史，不再手工同步第二份参数表。

```text
python3 tool/shell.py config show
python3 tool/shell.py config set --file <候选JSON文件> --context <状态版本> --actor <执行者> --request <请求号> --by <用户> --basis <依据>
```

候选文件放受管路径外。白名单仅接受 truth 内明确、无重复的普通 UTF-8 文本路径；不接通配符、越界或符号链接，不提供节选、摘要或降档。空表合法，表示没有 truth 文档入包，不表示递归全读。降容量低于当前占用即拒绝。白名单不是对象修改授权，也不能绕过禁止读取的上级边界。

若现有白名单文件丢失导致状态包无法生成，明确诊断后可走配置恢复：config show 返回 `edit_token`；config set 另用 `--recover --expect-source <edit_token>` 代替 context，仍需用户依据和稳定请求号。只允许改变配置，任务文件有误仍拒绝，先 repair。不要把恢复入口当任务写入旁路。手改配置导致不一致时，repair 会保留差异并按已登记配置重建。

## 成功回执与重试

任务/配置/升级操作返回 `files`，其中 `base` 为 Git 根，`ledger` 与各任务的 `task`、`approval`、`receipt`、`legacy`、`package` 为实际文件路径。没有的字段为 null。`stage_paths` 是本次操作涉及的文件（取消还含需要暂存删除的旧路径），不是整个工作树，更不是扩大授权。不要猜文件名；另外修改的项目资料须按范围一并暂存。

同号同业务输入重试返回原操作的路径与结果，允许原状态版本已经过期；`current_seq` 可能已前进。同号不同业务输入拒绝。先看 committed/already_applied，不重造请求；别为了“重试”重复批准、交付或升级。

## 提交、恢复和保障边界

同批暂存机器账、任务包/取消档案、配置、相关工具和交付工件，提交检查读实际索引。`.shell/local/` 全部排除；状态包不随 Git 分发，新机器从正本重建。运行时不负责自动提交、推送或发起模型会话。

`repair` 先保留意外差异，再重建任务文件与配置；缓存用 state get 重建。账本损坏、Git 历史冲突、旧备份不符则停止，不补造授权或丢失历史。进程锁退出会释放，不删除 queue.lock 抢锁。新协议与旧入口共用全部检查，无 context 的正常写入不能绕过。

退出码：成功 0；拒绝 1；`committed_pending` 为账已保存而投影未完成，退出 2，原请求重试或 repair。缓存完整不等于人或模型已读懂；同权限恶意改程序、直接写业务文件、绕过 Git 钩子不在保证内。只在同一主工作树写队列，不支持多主分支队列合并或自动工作树隔离。

## 升级已有机器账

先备份并运行 `upgrade --preview`；返回源指纹、阶段映射和窗口占用。确认后运行 `upgrade --expect-source <指纹> --actor <执行者> --request <请求号> --by <用户> --basis <升级依据>`。升级追加协议事件及配置历史，保存本机旧账备份，不改旧账前缀。旧在办任务按已有事实映射为登记/批准/领取/交付；旧收官映射通过；旧驳回只作历史，不再列为当前任务。旧封卷（包括旧驳回卷）留在原地址、字节不变；这只是历史兼容，不恢复其当前状态。

容量不足时预检拒绝，先明确调整接入容量或用原工具处理旧队列；不静默取消、通过或漏送任务。旧批准基线、原样回执不重写；旧工具会拒绝新协议事件，不能删字段或删新事件来降级。实际恢复旧版本应恢复整套已验证的升级前备份，并保留升级后的历史供核对，不用旧工具直接写新账。

更早的无附件候裁 Markdown 可先用旧 `migrate --preview`、`migrate --expect-source ...` 明确导入，再执行协议升级；批准态、终态或附件不明的 Markdown 不猜授权、不自动导入。
