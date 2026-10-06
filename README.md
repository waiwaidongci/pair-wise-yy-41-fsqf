# 桥梁限行发布审批

值班员报预警 → 桥梁工程师一级放行 → 安全监督员二级放行的限行通告审批系统，
包含路网容量联算、相邻桥绕行承接、发布额度并发占用、输入变化失效重算、
已发布快照固化、写入失败步骤恢复和紧急放行复核。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：角色、限行级别、通告状态、错误类型与基础校验。
- `src/rules.py`：两级放行状态机、角色矩阵、公交/救护/相邻桥/路网容量联算。
- `src/repository.py`：SQLite建表、发布额度唯一约束、版本控制、绕行承载、步骤恢复与审计链。
- `src/service.py`：权限检查、用例编排、快照、失效重算与审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：规则、完整流程、失败恢复与HTTP测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 角色与流程

角色：`duty_officer`（值班员）、`bridge_engineer`（桥梁工程师）、
`safety_supervisor`（安全监督员）、`traffic_ops`（交通调度）、`viewer`（只读）。

状态机：

```
pending_engineer ──工程师放行──▶ pending_supervisor ──监督员放行──▶ restricted ──卸载完成后恢复──▶ restored
      │                                 │
      └────────监督员紧急放行───────────┴──▶ emergency_pending_review
                                                 ├──复核通过──▶ restricted
                                                 └──复核驳回──▶ revoked（撤销发布并释放额度）
pending_* ──气象/交通通告/巡检变化──▶ invalidated（未发布失效重算，释放额度）
```

业务规则：

1. **两级放行**：值班员报预警后，必须依次经桥梁工程师、安全监督员放行才发布。
2. **容量联算**：封路占路网容量，公交绕行、救护通道预留与相邻桥限行折减一起核算；
   相邻桥已全桥封闭时绕行路径中断，核算不通过则监督员不能放行。
3. **恢复等待卸载**：相邻桥承接绕行流量时，原桥恢复申请必须等下游卸载完成。
4. **发布额度**：同一桥梁同时只能有一个在途审批；两名值班员同时提交时先通过者占用额度，
   后到者收到409并看到`holder`和`notice_id`（被谁、被哪条通告占用）。
5. **失效与快照**：气象、交通通告或巡检记录变化后，未发布通告失效重算并释放额度；
   已发布内容保留发布时点原始快照，不被重算覆盖。
6. **失败恢复**：提交/发布/恢复按步骤记账，写入失败后重跑从剩余步骤恢复，
   已占用额度只记一次，审计不重复。
7. **紧急放行**：安全监督员紧急放行必须绑定现场证据`evidence`与复核人`reviewer`
   （不能是放行人本人）；复核不通过则撤销发布、回滚桥状态、清空绕行并释放额度。

## 主要接口

- `GET  /health`
- `POST /api/bridges`（bridge_engineer/traffic_ops）；`GET /api/bridges`、`GET /api/bridges/{id}`
- `POST /api/bridges/{id}/context`：登记 weather/traffic_notice/inspection，自动失效在途通告
- `POST /api/notices`：值班员报预警，需提供幂等键`request_id`
- `GET  /api/notices`（支持`?bridge_id=&status=`）；`GET /api/notices/{id}`
- `POST /api/notices/{id}/engineer-release`（提交`expected_version`）
- `POST /api/notices/{id}/supervisor-release`（发布前重新容量核算）
- `POST /api/notices/{id}/emergency-release`（需`evidence`、`reviewer`）
- `POST /api/notices/{id}/review`（紧急复核，`approved`+`detail`）
- `POST /api/notices/{id}/offload`（交通调度上报卸载量）
- `POST /api/notices/{id}/restore`（绕行未卸载完会被拒绝）
- `GET  /api/notices/{id}/diversion`；`GET /api/audit`

## 测试

```bash
python3 -m unittest discover -s tests -v
```
