# 安静观测部署与回档

## 完成边界

源码测试通过不等于已运行新版本。部署必须依次验证 release SHA、扩展 build/instance/request receipt、任务专属真实强度 gate、Watchdog module_path 和 store_path、Observatory install.json，以及 registry 成员保持不变。

在所有三方源码检查和独立复核通过以前，不激活本轮 release。

## 已保存的回档点

| 组件 | Git 回档点 | 基线 |
| --- | --- | --- |
| Observatory | rollback/observatory-pre-quiet-20261002 | 6455cf8 |
| Watchdog | rollback/watchdog-pre-quiet-20261002 | 7052c11 |
| Sidecar | rollback/sidecar-pre-quiet-20261002 | d50c48e6fc712f1ed4e3518b2d58b574c057ce1b |

Watchdog 基线包括当时实际运行目录中的现代 UI 和机械两相改动，已经提交保存。仅回到 6d3d960 会丢失这些工作。

原始运行文件快照：`C:\Users\Administrator\AppData\Local\GPTObservatory\backups\pre-quiet-20261002-2210`。25 个文件 SHA256 与 SQLite integrity_check 已验证。用 `scripts/pc2-runtime-snapshot.py verify <snapshot>` 可再校验。

## 激活顺序

1. 读取实时 Watchdog /health、/watches，保存 exact instance_id、pid、module_path、store_path 和当前 desired 集合。当前任务不测试、注销或发送至生产会话。
2. 仅停止已确认的旧 Watchdog runtime PID；重新确认旧 listener 消失，保护同一 registry SQLite 文件。
3. Sidecar 源码完整提交后，用 canonical bootstrap 激活 immutable release，保留既有 Runtime Home、data_root 和 subagents Project。不要用 --live-check 在强度 gate 前发送。
4. 用稳定 CLI 的 extension-status 和 extension-update 验证新 instance、精确 build、同次请求 receipt。不能强制跳过 busy、清 pending/outbox 或手改 runtime/native 注册指针。
5. 在任务拥有的 exact tab 用 Sidecar 将推理强度改为 Medium；若原来已经 Medium，先改为其他非 Pro 强度再改回。独立回读可见 label 与 slider now/min/max。
6. Watchdog 从 clean Git commit 导出 immutable release；稳定 launcher 指向它，使用原 registry-v2.sqlite3。验证 module_path、store_path、membership、generation、health；已存在的 desired binding 只做 owner 派生恢复。
7. Observatory 使用 npm run app:install。安装器先 prepare、swap current/.previous、验证启动；失败自动回退。canonical 数据目录保留。
8. 仅用专属测试会话验证 bind/unbind provenance、确认回执、观察静默和实际效果撤回。EMPTY gate 在隔离 daemon 与本地计数探针上执行，不为制造空载而注销生产任务。
9. 最后确认 upstream behind=0，推送 reviewed commits，保留运行证据和回档点。

## 回档原则与操作

**安全回档默认停监督器、保留当前 desired registry 和业务数据。** 快照里的 watchdog-registry.sqlite3 只用于数据库灾难恢复；用它做普通代码回档，会让后来已经解绑的任务复活。

- Watchdog：先按 /health 身份停止新 runtime，恢复快照中的 watchdog-start.cmd 至稳定 launcher，默认保持监督器停止并保留 registry-v2.sqlite3。旧 runtime 字节和 7052c11 tag 均保留，但旧代码不理解 pending withdrawal，也没有显式注册门禁；不能直接启动并宣称新生命周期契约仍成立。若要恢复旧监督服务，须使用保留显式门禁的兼容回档 release，并重新做 owner drain、membership 和空载 gate。
- Sidecar：在独立 clean checkout 的 d50c48e6 基线运行 canonical bootstrap --activate，指定既有 Runtime Home 和同一 subagents Project，然后 extension-update 验证旧 build 的新 instance。回旧 owner 前，必须先在新 owner 证明 pending withdrawal 与 durable content-effect journal 均已真实收敛；未收敛时保持停机，不能丢掉屏障。保留 data_root；不手改 current_release 或 Native Messaging manifest。d50c48e6 的 WorkController 仍有旧式 auto-register，因此配套 Watchdog 必须保持停止，或者使用仍拒绝隐式注册的兼容 release。
- Observatory：安装器失败时自动恢复 .previous。部署成功后的主动回档应先停止已确认的 Observatory 服务，恢复已验证原运行快照或由 clean rollback tag 走 canonical app:install；启动后重新验证 install metadata、资产与数据目录。原运行快照可保留曾有热更新而 install metadata 未跟进的精确字节。
- 如果撤回或扩展升级显示 busy/unknown，先解释真实未收敛状态。不要把 ACK、命令超时、文件已复制或源码测试通过当作运行完成。

## 不可读页面与后台运行

页面缺少持久轮次身份时，保留 desired 注册，报告 `observation_unavailable / persistent_turn_identity_unavailable`；不自行刷新、发送或清除身份检查。相同失败只在状态首次出现时发 WARNING，原因或类型变化继续告警；恢复成功记录 INFO。失败计数、完整错误和最后成功时间持续更新，因此去重不等于隐藏故障。

激活器直接以 `pythonw.exe` 和 `CREATE_NO_WINDOW` 启动精确 release；稳定 launcher 使用后台 GUI Python，同一 registry 和 stdout/stderr 日志。运行验收要在至少两个 scheduler 周期中核对重复错误日志数量，并确认没有 Watchdog 控制台窗口。

## 长期 gate

运行 `python -m pytest -q tests/test_zero_browser_system_gate.py`：真实 --simple 子进程，隔离 SQLite、fake Sidecar 和 Relay 计数，覆盖 EMPTY、明确绑定恢复观察、解绑 ACK 后静默、隐式注册拒绝、重启仍空载。ACTIVE interval=0.03s，每段静默窗口0.25s。Gate 不访问真实浏览器。

其他边界测试覆盖同样本新鲜状态、未知 finality、注册代次、发送前 checkpoint、丢 ACK、pending withdrawal、shutdown wake、图接口失败和 UI 回执乱序。扩展 actual Promise drain 与跨重启未完成记录应单独保留。
