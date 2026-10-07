# 手动重绑闭环：候选实现验证记录

日期：2026-10-07（PC2，UTC+8）。这是候选代码，不是现网验收完成声明。

## 用户目标与边界

手动绑定不仅要建立机械任务，还要真正清理旧正常绑定、重新绑定并验证。允许清理残余绑定；不删除对话历史，不绕过人类暂停或未决发送，不把机械兜底重新塞回正常状态机。

## 实现

1. 保留原有同步机械挂载屏障。
2. 显式绑定登记 request_rebind，并关联 operation_id 与 registration_id。
3. 正常 poll 在原 entry.lock 下排空在途工作，持久化最后运行状态，关闭旧 watcher 并清空缓存。
4. 重新构造 watcher、重新调用 Sidecar bind，恢复同代次运行状态。
5. 第一个 tick 只执行 observe_once；不发送 continuation/REVIEW，不解除人类暂停。只有读到完整当前轮次身份和可读 browser observation 才确认本次 probe。
6. 新请求不会被旧 poll 结果覆盖；注销后的请求和旧 registration_id 请求不能复活或修改新代次。
7. 将 NEED_INPUT 的 latch/user-turn 信息纳入 durable state，重建时不丢暂停；DONE、未决 REVIEW 和发送保留记录继续保留。
8. task projection 在自身锁内读取当前 registry 快照，避免旧快照删掉刚创建的机械任务。

## 观测站独立仓库变更

`server/watchdog.ts` 保留 mechanicalAttached 与 normalBinding 回执，并排除 rebinding/observation_unavailable/readable=false 的 operational 误判。

`src/App.tsx` 增加每个任务卡的“重新挂载”按钮。绑定反馈与任务卡分开显示“正在重新挂载 / 已取得观测 / 观测未通过”。“注册已确认”不再等于“正常监督就绪”。

`src/task-status.ts` 的纯展示函数由测试覆盖。此仓库在 Birdview 图中是外部组件；主仓库的路径所有权校验不声称覆盖它。

## 已执行验证

- Watchdog：387 tests passed，87 subtests passed；compileall 通过。
- Observatory：50 tests passed；TypeScript、Vite、服务端构建及 lint 通过。构建仍有既有单 bundle 大小警告。
- 新增失败用例先在缺陷版本观察到失败，再实现修复。
- `fixed_action_timer.py`、`model.py`、`relay_page.py` 与 ff29b57 没有差异。
- 还原点 runtime-snapshot：25 个文件已验证，SQLite integrity=ok。
- Birdview：9 模块、10 关系、2 条活动记录，schema/authoring 验证无错误无警告；HTML 已生成，但未完成用户可见浏览器视觉验收。

## 实机门限与限制

未向生产对话发送测试提示词，未改动生产 conversation 的注册。尝试创建专用测试对话时，Sidecar 返回 `ChatGPT Project anchor was not found`。在该测试根页上，Sidecar 的 Pro→Medium 切换与独立 Relay DOM 读回曾成功（slider 1/0/4）。导航到 subagents 项目页后，High/Medium 强度控件没有完成读回；随后进一步 DOM 检查被工具拦截，已停止这条浏览器操作路径。

后续一次 Observatory 的重复 test/build/lint 组合调用也因工具无法确定安全状态被拦截，未继续重试；前述 50 项测试与构建/lint 为此前实际成功结果。

因此：真实测试对话的正常重绑、刷新后重新观测、机械任务的实际发送/终态跳过，以及该候选 UI 的实机点击验收均尚未完成。不得由测试数量或 GUI 构建成功推出这些能力已经通过。

## 保存与回退

主仓库还原分支：`backup/pre-manual-rebind-20261007` → ff29b57。
观测站还原分支：`backup/pre-manual-rebind-20261007` → e1a7d45。
机器快照：`%LOCALAPPDATA%\chat-watchdog\restore-points\20261007-pre-manual-rebind\runtime-snapshot`。

在实机门限通过前，候选实现只保存为代码分支，不替换现有活动入口。代码回退不得盲目覆盖后来新增的注册记录；新版暂停状态字段遇旧版本时应保持 fail-closed，不能为了通过旧解析器而丢弃人类暂停状态。

## 后续唯一验收入口

使用一条明确归属于本任务的、已经建立持久 conversation URL 的测试对话，在该对话上完成 Sidecar Medium 切换及独立读回，然后验证一次绑定→清旧缓存→重绑→正常观测回执，以及独立机械路径。不要用业务对话充当测试样本。
