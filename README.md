# 中亚合作项目承诺协调

承接辽洽会等经贸对接活动后的主体核验、需求供给匹配、资源暂留与项目履约。
目标是替代"联系人清单 + 口头承诺"的做法，保证同一土地、仓容、班列窗口与
专家支持不会被不同项目重复承诺。

## 领域边界

系统服务四类参与者：辽宁企业、中亚合作企业、商协会秘书处、地方项目服务机构。
对接方向包含本地化生产、陆港共建（含跨境物流）、农产品种植与精深加工。

## 核心规则如何落地

| 业务要求 | 实现方式 |
| --- | --- |
| 主体版本与授权联系人 | 主体资料为不可变版本流（`parties.py`），意向与承诺引用具体版本号；联系人离任只标记，历史动作保留 |
| 意向转可比较需求/供给 | 七维条款归一化（`clauses.py`）：投资阶段、属地条件、物流资源、农业原料、加工能力、币种口径、保密范围 |
| 候选匹配列满足项/缺口/冲突 | `matching.evaluate_pair` 双向逐维比较，并探测组合资源冲突；匹配只读不占资源 |
| 双方确认后才有截止时间暂留 | 承诺 proposed → held；两个不同主体的授权联系人都确认后才生成 deadline |
| 组合原子占用 | 土地/仓容/班列/专家四类资源同一把锁内一次占用，任一不足整组拒绝（`resources.py`） |
| 改量/延期/终止只释放未履行份额 | 占用分 held/fulfilled/released 三量；已交接数量与费用永久留存 |
| 尽调按用途授权 | 授权绑定用途、材料清单与到期时间；超用途/超范围访问被拒 |
| 译文更正保留原文 | 材料版本链追加，原文不可删除，新版本以 `corrects_version` 指向原文 |
| 联系人离任 | 仅终止其尚未完成的尽调访问；已完成访问与历史授权留存 |
| 重放不产生第二份承诺 | 所有产生承诺/资源效果的命令携带 `request_key`，入口预查 + 写入指纹双保险 |
| 跨时区争用单一结果 | 确认等命令在文件锁内基于最新版本重新决策（`store.commit_unit`），乐观并发版本拒绝过期写入 |
| 人工调整职责分离 | 调整"申请人—复核人"分离；发起人不能复核，申请人不能复核自己 |
| 系统恢复后续跑 | 状态全部由只追加事件日志重建；`run_due_jobs` 处理暂留到期、尽调待办、履约提醒，可重复幂等运行 |
| 项目接口 | `project_view` 直接给出当前责任人、资源缺口、历次承诺、最终落地/终止原因 |

## 模块结构

```
src/central_asia_project_commitment/
├── clock.py          # UTC 存储 + IANA 时区呈现；可替换/固定时钟
├── errors.py         # 版本冲突、状态冲突、资源冲突、策略违反、幂等重放
├── store.py          # JSONL 只追加事件存储：流版本、文件锁、幂等键表、锁内决策
├── aggregates.py     # 聚合基类与仓储
├── parties.py        # 主体（四类）不可变版本与授权联系人
├── clauses.py        # 七维需求/供给条款归一化与逐维比较
├── intents.py        # 合作意向（需求侧 + 供给侧）
├── resources.py      # 土地/仓容/班列/专家资源池与区间容量账本
├── matching.py       # 候选匹配：满足项、缺口、资源冲突
├── commitments.py    # 承诺暂留：双方确认、截止时间、交接、调整申请/复核、终态
├── duediligence.py   # 尽调案件：材料版本链、用途授权、访问、离任终止
├── projects.py       # 项目（关联意向与尽调案件）
├── projections.py    # 读模型：事件重放重建 + 项目接口视图
└── backend.py        # 应用服务门面，编排全部用例与恢复任务
```

所有状态都是事件流的投影；进程重启或系统恢复后重放事件日志即可得到完整状态，
无需额外数据库。存储默认是带跨进程文件锁的 JSONL（`EventStore`），可替换为
同样实现 `read_stream` / `commit_unit` 的其他后端。

## 典型流程

```python
from datetime import timedelta
from central_asia_project_commitment import Backend, Contact, EventStore, SystemClock

backend = Backend(EventStore("data/events.jsonl"), SystemClock())

# 1) 主体版本 + 授权联系人
backend.register_party("ln-equip", "liaoning_enterprise", "辽丰装备", "Asia/Shanghai",
                       {"capital": "5000万"}, country="CN")
backend.add_contact("ln-equip", Contact("c-ln", "王磊", "海外投资部长"))

# 2) 意向：七维需求/供给
backend.file_intent("i-ln", "ln-equip", "c-ln", "localized_production", "面粉本地化",
                    demands, offers, request_key="intent-ln-001")

# 3) 候选匹配：满足项 / 缺口 / 资源冲突（只读）
result = backend.match("i-ln", "i-kz", candidate_bundle)

# 4) 双方确认后暂留（带 request_key，重放安全）
backend.propose_commitment("cmt-1", "i-ln", "i-kz", "ln-equip", "c-ln", "c-kz",
                           bundle, ttl_seconds=7 * 24 * 3600, request_key="cmt-001")
backend.confirm_commitment("cmt-1", "ln-equip", "c-ln", request_key="conf-a")
backend.confirm_commitment("cmt-1", "kz-agro", "c-kz", request_key="conf-b")

# 5) 履约交接（数量与费用留存）；人工调整须他人复核
backend.register_handover("cmt-1", "land-sy", 100, "HO-001", 200_000, request_key="ho-001")
backend.request_adjustment("cmt-1", "adj-1", "resize", "c-sec", {"bundle": new_bundle})
backend.approve_adjustment("cmt-1", "adj-1", "c-local")  # c-local 未参与发起

# 6) 系统恢复后扫描（可随时、重复运行）
backend.run_due_jobs()

# 7) 项目接口
backend.project_view("prj-1")
# {current_responsible, resource_gaps, commitment_history, final_outcomes, ...}
```

## 时间与时区

内部一律 UTC 存储；截止时间、审批时刻按联系人所在 IANA 时区呈现
（`clock.format_in`）。暂留 TTL 在确认完成那一刻起算。

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译检查：`python3 -m compileall -q src`

测试包含：

- `tests/test_context.py`：领域资料读取；
- `tests/test_liaoning_scenario.py`：辽洽会综合端到端场景（主体版本、匹配、
  双方确认、原子占用、重放幂等、争用单一结果、改量/终止份额留存、尽调授权、
  译文更正、离任、恢复续跑、项目视图）；
- `tests/test_concurrency.py`：跨进程真实并发的确认争用与重放。

上述命令只读写仓库内临时文件，不连接外部业务服务。
