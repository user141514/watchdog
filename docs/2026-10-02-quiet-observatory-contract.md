# pc2 安静监督系统契约

用户目标：观测站明确管理绑定生命周期；常规观察不 attach 浏览器；解绑不隐式复活，且每次显示成功必须有权威状态证据。

## 当前基线与所有权
- Observatory source 6455cf8，origin/main 仅领先2，不落后；回档 tag rollback/observatory-pre-quiet-20261002。
- Watchdog deployed source 6d3d960 + 13 files modern-UI/机械两相修复已保存为7052c11；回档 tag rollback/watchdog-pre-quiet-20261002。部署目录是基础设施，隔离开发 worktree 以此为base。
- Sidecar deployed immutable release d50c48e6fc712f1ed4e3518b2d58b574c057ce1b；回档 tag rollback/sidecar-pre-quiet-20261002，源码重放至当前main并保留必要UI修复。
- Registry owns desired membership and durable phase state; Sidecar owns observed ConversationState and browser effects; Observatory is derived view + explicit command proxy.

## 不变量
1. 常规 Watchdog/Observatory observation 不向Relay发送请求，不调用CDP，不创建/刷新/发送标签页。
2. 相同新鲜观测内的exact conversation identity、stateVersion、writerEpoch绑定写intent；没有新鲜readable observation、identity不符或未知finality时不写。
3. Simple 机械模式中 ACTION phase0 只转 REVIEW phase1；只有当前 registration_id 下明确请求的 REVIEW（持久 expected intent 关联）及其有效 DONE JSON 才能终止。人工/其他来源的新轮次不能充当 REVIEW；关联未知时 NEED_INPUT 暂停，只有明确新绑定才重置监督阶段。所有正常模式外部生命周期不因 DONE 自动注销。
4. 900s有实际停滞证据才恢复；所有stop/continue/refresh由Sidecar执行，未知ACK不重放。
5. Registry active_count=0 且 pending_withdrawal_count=0 时 scheduler 无限 Event.wait；注册/注销/关闭唤醒，真正空载 health 仍 ready。撤回未确认时只重试 owner 清理，不能冒充 EMPTY。
6. /register需explicit bind和完整source/actor/operation_id/reason，未声明或自动路径拒绝。来源字段是审计声明，不能当认证凭据。
7. provenance与membership事务持久化；旧来源标为unknown，不能伪造human历史。
8. 每代registration_id由Registry持久化；Sidecar接收派生grant，解绑先撤销该代并等待已接受的实际writer收敛，再返回成功ACK。超时保持durable pending withdrawal并返回未知，不能冒充已完成。ACK后旧代无浏览器作用、无registry复活；只有新explicit bind可恢复。
9. UI mutation后获取全新成功registry snapshot再核验exact membership；旧inFlight/网络失败不冒充完成。解绑回执保留，图里只显示当前监督集合。
10. 回档默认只回代码/配置，不回滚desired registry数据，避免恢复用户已解绑的任务。

## 反例与验收
- 旧terminal state + 本次observation未知 => 不发。
- mutation期间的旧sync晚返回 => 不可确认成功。
- last unregister与scheduler sleep竞态 => first bind不丢唤醒。
- 旧WorkController自动重绑 + 先注销 => /register拒绝自动复活。
- lost ACK + daemon restart => 同一intent不重复发送；phase+consumed assistant turn在effect前持久化，旧ACTION的DONE JSON不冒充新REVIEW结果。
- managed observer原始finality未知 + cached terminal => 不发、不终止。
- owner执行仍在进行 + Watchdog HTTP超时 => 解绑不得先返回成功；撤销/收敛覆盖实际content-script writer promises。
- 已请求 REVIEW 后的新人工轮次输出 DONE => 不能终止旧 REVIEW；未匹配持久因果关联时暂停。
- 扩展后台重启丢内存 Promise => 持久 content-effect 记录仍阻止提前 quiescence ACK，不能用超时清除。
- EMPTY连续观测 => Relay请求、CDP下游连接、刷新、send、target creation均为0。
- 实机先精确task-owned test tab的Sidecar强度切换再独立DOM回读；代码/扩展/daemon更新后重读authority。

## 执行顺序
基线回档 -> RED行为测试 -> 最小owner接口 -> 并行组件实现 -> GREEN与build/quality -> 独立review -> 隔离loopback集成与实机gate -> commit/deploy -> 重读runtime/build/registry -> behind=0 -> 回档说明。
