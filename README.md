# 桥梁结构监测与限行决策

融合传感、巡检、交通荷载和天气数据，生成限载限行或恢复建议。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8318
```

默认端口为`8318`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：sensor_operator, bridge_engineer, traffic_authority, viewer, duty_officer, safety_supervisor, reviewer。监测偏差与预警阈值之比和多条异常记录决定告警等级；限行与封闭决策必须绑定交通通告记录。

## 限行通告审批流程

桥梁限行先由值班员（duty_officer）报预警，再经桥梁工程师（bridge_engineer）和安全监督员（safety_supervisor）两级放行。

状态机：`pending_approval` → `engineer_approved` → `supervisor_approved` → `published`；条件变化时未发布通告失效（`invalidated`），紧急放行复核不通过则撤销（`revoked`），恢复后为`restored`。

### 主要接口

- `POST /api/bridges/{id}/notices`：值班员发起限行通告
- `GET /api/bridges/{id}/notices`：列出该桥梁的限行通告
- `GET /api/notices/{id}`：查看通告详情
- `POST /api/notices/{id}/approve`：两级审批（工程师→监督员）
- `POST /api/notices/{id}/publish`：发布限行通告（占用额度、保存快照）
- `POST /api/notices/{id}/emergency-publish`：安全监督员紧急放行（绑定证据+复核人）
- `POST /api/notices/{id}/review`：复核紧急放行（不通过则撤销+释放额度）
- `POST /api/notices/{id}/restore`：恢复通行（需下游卸载完成）
- `POST /api/notices/{id}/recover`：从写入失败的剩余步骤恢复
- `GET /api/bridges/{id}/quota`：查看发布额度占用情况
- `POST /api/bridges/{id}/detours`：添加绕行路线（公交/救护/相邻桥）
- `GET /api/bridges/{id}/detours`：列出绕行路线

### 业务规则

1. **两级放行**：限行通告须经桥梁工程师和安全监督员依次审批后方可发布。
2. **路网容量核算**：封路占路网容量，公交绕行、救护通道和相邻桥梁限行一起核算；容量不足或相邻桥超限则拒绝发布。
3. **下游卸载**：相邻桥承接绕行流量时，原桥恢复申请须等下游桥卸载完成。
4. **发布额度**：两名值班员同时提交同一桥梁告警，先通过者占用发布额度，后到者可见占用者信息。
5. **失效与快照**：气象、交通通告或巡检记录变化后，未发布通告失效重算；已公布内容保留原始快照。
6. **失败恢复**：写入失败后从剩余步骤恢复，已占用额度只记一次（幂等）。
7. **紧急放行**：安全监督员紧急放行须绑定现场证据和复核人；复核不通过则撤销发布并释放额度。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
