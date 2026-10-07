# 省级数字人才培训调度系统

面向培训机构、企业需求、学员能力档案与年度名额的一体化后端调度系统。
接收带**技能等级与地域约束**的培训需求，为课程建立**报名 / 候补 / 出勤 / 结业**全状态，
依据**机构容量、企业优先级、年度配额**生成**可解释**的分配结果；学员转班、机构停课、
名额释放、跨年度结转、重复导入均保持**历史可审计**；服务重启后未完成的候补处理自动续跑。

纯 Python 3.11 标准库实现（sqlite3 + http.server + threading），无第三方依赖。

## 快速开始

```bash
# 运行全部测试
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 端到端演示（高优先级抢占、候补、取消回收、审计校验）
PYTHONPATH=src python3 -m talent_training.cli demo --db data/demo.db

# 启动 HTTP 服务
PYTHONPATH=src python3 -m talent_training.cli serve --host 0.0.0.0 --port 8080 --db data/talent.db

# 校验审计哈希链
PYTHONPATH=src python3 -m talent_training.cli verify --db data/talent.db
```

## 数据模型

| 表 | 含义 |
|---|---|
| `provider` / `enterprise` / `student` / `class_session` | 机构、企业、学员能力档案、班级 |
| `training_need` + `need_candidate` | 培训需求（技能/等级/地域/人数/年度）与候选学员序 |
| `enrollment` | 报名记录，状态机贯穿全生命周期，转班用 `transferred_from` 串联，永不物理删除 |
| `allocation_run` + `allocation_decision` | 每轮分配及逐人决策，决策留存完整规则轨迹 |
| `quota` + `quota_ledger` | 年度配额当前值与只追加台账（consume/release/adjust/carry_in） |
| `waitlist_entry` | 持久化候补队列（pending/processing/placed/cancelled/dead） |
| `carryover` | 跨年度结转记录，同一对年度维度只允许一次 |
| `import_batch` | 批量导入批次与去重统计 |
| `audit_log` | 哈希链审计日志，只能追加、篡改可被发现 |

## 分配规则与可解释性

每位候选学员依次过规则，每步记录规则名、输入、通过与否、拒绝原因，
写入 `allocation_decision.trace_json`，可通过 `GET /api/allocations/{run_id}` 复盘：

| 规则 | 内容 |
|---|---|
| R1 需求有效 | 需求仍开放、未超过需求人数 |
| R2 无重复安排 | 同学员同技能同年度不得存在有效安排（解决"同一学员被重复安排"） |
| R3 能力前置 | 当前等级 < 目标等级，且差距不超过最大可培养跨度（默认 2 级） |
| R4 课程匹配 | 年度内存在同技能同等级班级 |
| R5 地域约束 | 班级地域必须在需求允许地域内 |
| R6 机构状态 | 机构在营且班级开放报名 |
| R7 班级容量 | 班级尚有余量（按在籍报名实时计算） |
| R8 年度配额 | 该年度/地域/技能尚有配额 |

- 企业按 `priority DESC` 排序处理，紧缺工厂优先；同企业内按候选学员顺序。
- 所有规则通过 → `registered` 并消耗 1 个班级名额与年度配额；
  仅因 R7/R8（容量/配额）不满足 → `waitlisted` 进候补；
  命中硬性规则不满足 → `rejected`，原因可追溯到具体规则。

## 候补队列与重启续跑

- 候补条目全部落库。后台工作线程按**企业优先级、登记时间**轮询，
  课程取消 / 转班 / 配额调整后会被立即唤醒重试。
- 状态机 `pending → processing → placed`。服务启动时
  （`WaitlistWorker.recover`）把崩溃残留在 `processing` 的条目复位为 `pending` 并继续，
  保证宕机不丢候补。

## 名额释放与结转

- 课程取消：所有在籍名额回收（`quota_ledger` 记 release 流水），学员回候补等待重排。
- 机构停课：名下开放班级全部暂停、名额统一回收、学员转候补。
- 学员转班：旧班释放、新班占用，两条报名记录串联，台账净值守恒。
- 跨年度结转：`POST /api/quotas/carry-over`，未使用配额写入下年 `carry_in`，
  同一对年度不可重复结转。

## 审计

所有写操作追加到哈希链：每条记录包含前一条的 SHA-256 哈希。
`POST /api/audit/verify` 重算全链，可发现删除或改写（测试 `test_audit_chain_detects_tampering` 覆盖）。

## 批量导入去重

`POST /api/imports` 一次导入学员与需求，整批事务：
- 学员按证件号去重；
- 需求按业务字段指纹（企业/技能/等级/年度/地域/人数/候选集）幂等去重；
- 返回新增/重复计数，并写批次审计。

## HTTP API 摘要

```
POST/GET /api/providers | /enterprises | /students | /classes | /needs
GET      /api/students/{id} /api/classes/{id} /api/needs/{id}
POST     /api/imports
POST/GET /api/quotas            GET /api/ledger
POST     /api/quotas/carry-over
POST     /api/allocations/run   GET /api/allocations   GET /api/allocations/{id}
POST     /api/classes/{id}/cancel
POST     /api/providers/{id}/suspend | /resume
POST     /api/enrollments/{id}/attendance | /complete | /cancel | /transfer
GET      /api/enrollments
GET      /api/waitlist          POST /api/waitlist/process
GET      /api/audit             POST /api/audit/verify
GET      /api/health
```

写接口接受 `X-Actor` 头标识操作人。示例：

```bash
curl -X POST localhost:8080/api/allocations/run \
  -H 'Content-Type: application/json' -H 'X-Actor: zhao' -d '{"note":"年度首轮"}'
curl localhost:8080/api/allocations/run_xxx   # 查看该轮使用了哪些规则
```

## 代码结构

```
src/talent_training/
  db.py          表结构与连接
  util.py        时间/ID/JSON 工具
  audit.py       哈希链审计
  entities.py    实体登记与批量导入去重
  quota.py       配额台账与结转
  allocator.py   规则引擎与分配运行
  operations.py  出勤/结业/退课/转班/停课/取消
  waitlist.py    持久化候补工作线程与崩溃恢复
  service.py     门面：统一锁、事务、线程生命周期
  api.py         HTTP API
  cli.py         serve / demo / verify
```
