# ClawForce 侧需要做的一次性配置

> Daycare 站点这边 4 个协议文件 + create_task.py 已就绪。要让 `python3 create_task.py "Cadence Education"` 真的跑通，ClawForce 后端还需要做以下 3 件事——**只有 owner / admin 能做**。

---

## 1. 注册新 task_type：`daycare_due_diligence`

`task_type_configs` 表加一行（参 [reference_seedforge_config_table](memory/reference_seedforge_config_table.md)：fetcher_enabled flag 在 `task_type_configs` 里，ConfigService 会永久 cache，改完 flag 必须 rollout restart pod）。

字段建议：

```sql
INSERT INTO task_type_configs (
  task_type,
  display_name,
  description,
  fetcher_enabled,           -- 关键: false（不走自动 fetcher，由 API 主动创建）
  api_creation_allowed,      -- 关键: true
  brief_template_id,         -- 指向新建的 brief 模板（见 §2）
  qa_gates,                  -- JSON: 至少 ["url_liveness", "schema_validity"]
  auto_verify_sources,       -- JSON whitelist
  budget_default,
  created_at
) VALUES (
  'daycare_due_diligence',
  'Daycare Due Diligence',
  'Background-check a U.S. daycare: ownership, incidents, federal investigations.',
  false,
  true,
  '<brief_id_from_§2>',
  '["url_liveness", "schema_validity", "source_tier_floor_t3"]'::jsonb,
  '[]'::jsonb,
  '0.05',
  now()
);
```

**关注点**：
- `fetcher_enabled=false` 是与 OL / MB / Wikipedia ZH 等任务类型的本质区别——我们**不**让 seedforge 自动注入，**只**接受 API 创建
- 改完 flag 后 **kubectl rollout restart** clawforce 容器（[memory: feedback_seedforge_deploy_always_gke](memory/feedback_seedforge_deploy_always_gke.md)）

---

## 2. 写 brief 模板（lobster 拿到任务后看的指令）

这是 lobster 实际执行时跟随的指南。最小版本：

```markdown
# Daycare Due Diligence — Lobster Brief (v0.1)

## Task

Given the daycare name in `structured_spec.daycare_name`, identify:
1. Operating brand (the legal entity / public-facing chain name)
2. Parent company (immediate corporate parent)
3. Ultimate owner (PE firm / public company / individual / nonprofit)
4. Owner type ("private_equity" | "public_company" | "franchise_pe" | "independent" | "nonprofit" | "unknown")

## Hard rules

- Every claim **must** include at least one URL source
- Sources must be Tier 1 (gov / SEC / court) or Tier 2 (mainstream media)
- If you cannot find a source, output `owner_type: "unknown"` — do NOT invent
- Use direct quotes from the source where possible
- Embed the marker `[ACG #sm_xxxxxxxx]` in the artifact metadata

## Output schema

```json
{
  "operating_brand": "string",
  "parent_company": "string",
  "ultimate_owner": "string",
  "owner_type": "string",
  "acquisition_history": "string (1-2 sentences if known)",
  "sources": [
    {
      "tier": "T1|T2|T3",
      "url": "https://...",
      "publisher": "string",
      "verbatim_quote": "string"
    }
  ],
  "confidence": "high|medium|low",
  "gaps_acknowledged": ["..."]
}
```

## Tools

- `web_search` — for finding ownership info
- `web_fetch` — for verifying sources are reachable
- Total tool calls: max 10
```

存到 brief 表 / `guides` 目录下，拿 brief_id 填回 §1 的 task_type_configs.brief_template_id。

---

## 3. （可选）注册 PublicGoodProject 记录

让 daycarecheck 显示在 agentic-commons 公益项目目录里。参 [project_wikipedia_translation_zh_created_20260601](memory/project_wikipedia_translation_zh_created_20260601.md) 的流程：

```
POST /api/admin/public-good-projects
{
  "source_kind": "daycare_due_diligence",
  "name": "Background-Check Your Daycare",
  "description": "Free, sourced background-check tool for U.S. families choosing a daycare.",
  "homepage": "https://daycarecheck.example.org",
  "license": "CC0-1.0",
  "task_type": "daycare_due_diligence",
  "guide_config": {
    "qa": {"override_whitelist": ["url_liveness", "schema_validity"]},
    "marker_policy": "sm_or_task_alias"
  }
}
→ {"id": "ppg_xxx", "ac_id": "..."}
```

然后 SQL approve（同 wikipedia translation 流程）。

如果不做 §3，create_task.py 仍能创建 task，只是 task 不与一个 PublicGoodProject 关联——agentic-commons.org 上不会出现这个项目入口。

---

## 4. 给 daycarecheck 站点一份认证

我们这边运行 create_task.py 需要：

| 凭证 | 来源 | 用法 |
|---|---|---|
| `CLAWFORCE_JWT` | admin 登录 → `/api/auth/...` 拿 access_token | 调 POST /api/agents 注册 client agent 用 |
| `CLAWFORCE_CLIENT_AGENT_ID` | 上一步 register_client_agent_once 返回的 id | 之后每次 POST /api/tasks 必带 X-Agent-Id |

或者直接给我一个 lf_xxx API key（针对 client agent 类型），就不需要 JWT 了——但 INTEGRATION_CONTRACT 里说 lf_xxx 是给 lobster 节点用的，client agent 用 JWT。**等你拍板**。

---

## 验收标准

跑完上面 §1-§4 后：

```bash
export CLAWFORCE_BASE=https://clawgrid.ai
export CLAWFORCE_JWT=<...>
export CLAWFORCE_CLIENT_AGENT_ID=<...>

python3 create_task.py "Cadence Education" --location "Park Slope, Brooklyn"
```

**期望输出**：
```
=== task created ===
{
  "id": "task_xxxxxxxx",
  "ac_id": "01HXYZ...",
  "alias": "AC-T-XXXXXXX",
  "status": "draft",
  "task_type": "daycare_due_diligence",
  ...
}
```

拿到 `ac_id` 即证明全链路 (1) 协议文件 + (2) API 创建任务 已通。后续的 lobster 拾取 / artifact / verify / 渲染 都是 next phase 的事。
