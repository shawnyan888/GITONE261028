# ONES 缺陷属性批量修改脚本 实施计划（plan-a）

来源需求：`aiai.md`
目标产物：单文件脚本 `ONES/update_bug.py`，配套缓存 `ONES/ones_field_cache.json`
本文档定位：可直接照做的实施计划。所有接口路径、字段名、参数、退出码、缓存结构均已在本文给死，实施阶段不需要再做设计决策。

---

## 1. 目标与已核实事实

### 1.1 目标

一条命令修改 ONES 中一个或几个 issue 的若干属性：

```shell
python update_bug.py --bug FUCO-7 FUCO-4 --leixing "安全问题" --chengdu "致命" --lingling "fusion2026.1.100.01" --force --yes
```

- `--leixing` 对应缺陷类型（单选）
- `--chengdu` 对应严重程度（单选）
- `--lingling` 对应 `ling-build-version`
- 默认只预览（DRY-RUN），`--yes` 才真正写库

### 1.2 已核实的 ONES Open API 契约

以下内容已对照官方文档 `developer.ones.cn/zh-CN/docs/abilities/openapi/api/*` 核对（接口首次加入版本 v3.14.117 / v3.15.26，文档版本 2.0）：

| 用途 | 方法 | 路径（相对 `{host}/openapi/v2`） | 关键参数 | 关键返回 |
| --- | --- | --- | --- | --- |
| 项目列表 | GET | `/project/projects` | `teamID` | `data.list[].{id, name, ...}` |
| 工作项属性列表（含单选项） | GET | `/project/issueFields` | `teamID`、`limit<=500`、`cursor` | `data.list[].{id, name, builtIn, typeEnum, options[].{id, value}}` |
| 工作项列表 | GET | `/project/issues` | `teamID`、`projectID`、`issueTypeID`、`limit<=100`、`cursor` | `data.list[].{id, number, project{id,name}, title, fieldValues[]}` |
| 工作项详情 | GET | `/project/issues/{issueID}` | `teamID` | `data.fieldValues[].{fieldID, type, value}` |
| 更新工作项 | PUT | `/project/issues/{issueID}` | `teamID`，body 见下 | `data.{id, number, title, projectID, issueTypeID, parentID}` |

更新请求体：

```json
{ "fieldValues": [ { "fieldID": "<属性ID>", "type": 1, "value": "<选项ID或选项值>" } ] }
```

- `value` 传 `null` 或不传 `value` 键 = 清空该属性（v6.18.25 起支持），本脚本不使用该能力，禁止传 null。
- 响应统一包一层：成功 `{"result":"SUCCESS","data":{...}}`，失败 `{"result":"FAIL","errorCode":"...","errorMsg":"..."}`。

属性类型枚举（`typeEnum` / `type`）：

| 值 | 含义 | 值 | 含义 |
| --- | --- | --- | --- |
| 1 | 单选 | 6 | 时间 |
| 2 | 文本 | 8 | 用户 |
| 3 | 整数 | 13 | 用户列表 |
| 4 | 浮点数 | 14 | 数字 |
| 5 | 日期 | 16 | 多选 |
| 19 | 布尔值 | | |

关键结论：`GET /project/issueFields` 一次性返回"属性名 + 属性ID + 类型 + 全部选项"，因此缺陷类型与严重程度的合法单选列表只需一次请求即可拿到，不需要额外接口。

---

## 2. 第 0 步：认证与端点核实（实施第一个动作，只读）

`ONES_BUG` 目前未在环境中设置，实例 host 与认证头形式都未实测，因此脚本必须自带一次只读探测，并把探测结果固化进缓存。

### 2.1 探测顺序（固定，不得改变）

对同一请求 `GET {host}/openapi/v2/project/issueFields?teamID={teamID}&limit=500`，依次尝试三种认证头：

| 顺序 | 请求头组合 |
| --- | --- |
| 1 | `Authorization: Bearer {ONES_API}` |
| 2 | `Ones-Auth-Token: {ONES_API}` |
| 3 | `Ones-Auth-Token: {ONES_API}` + `Ones-User-Id: {ONES_USER_ID}`（`ONES_USER_ID` 为可选环境变量，未设置则跳过该组合） |

判定规则：

| 结果 | 含义 | 处理 |
| --- | --- | --- |
| HTTP 200 且 `result=SUCCESS` | 命中 | 记录该组合到 `_meta.auth_header`，探测结束 |
| HTTP 401/403 | 认证失败 | 试下一组合 |
| HTTP 404 | 路径错误（host 或前缀不对） | 直接终止，报 `ENDPOINT_NOT_FOUND`，退出码 3 |
| 其它（5xx/超时） | 服务异常 | 重试 3 次后终止，退出码 3 |

### 2.2 探测结果的落地

- 命中组合写入缓存 `_meta.auth_header`（值形如 `Authorization: Bearer`、`Ones-Auth-Token`、`Ones-Auth-Token+Ones-User-Id`）。
- 命中后立即校验 `issueFields` 中能否按名字定位到目标属性（见第 6.3 节），定位不到则退出码 1 并打印全部属性名。
- 三种组合全部失败：报 `AUTH_FAILED`，打印最后一次响应体全文，退出码 3。
- `--force` 会重新执行本节探测；非 `--force` 且有有效缓存时跳过探测。

### 2.3 同时验证 issue key 是否可直接使用

探测完成后追加一次只读验证：`GET /project/issues/{issueKey}?teamID=`（`issueKey` 取 `ONES_BUG` URL 末尾段）。

- 若 200 且 `data.id` 存在：`_meta.issue_key_supported = true`，后续直接用 key 当 `issueID`，无需翻页。
- 若 400/404：`_meta.issue_key_supported = false`，走第 6.4 节的"项目内翻页解析"。此分支不得在运行时反复试探，一律以缓存里的标记为准，只有 `--force` 才重新判定。

---

## 3. URL 解析规则（从 `ONES_BUG`）

示例：

```
https://ones.cn/project/#/team/KKv1z9x9/project/FUCO/component/6zAPyNY5/nav/Ur8qw1B82iFXfYVV/view/N1D6osqb/issue/FUCO-7
```

提取规则：

| 变量 | 正则 / 规则 | 示例值 |
| --- | --- | --- |
| `host` | `^(https?://[^/]+)` | `https://ones.cn` |
| `teamID` | `/team/([^/?#]+)` | `KKv1z9x9` |
| `projectRef` | `/project/([^/?#]+)`（取第一次出现） | `FUCO` |
| `issueKey` | `/issue/([^/?#]+)`（缺省时取 URL 最后一段） | `FUCO-7` |

校验：

1. `host`、`teamID`、`projectRef` 三者在 URL 中缺失 → 报 `BAD_ONES_BUG_URL`，退出码 2。
2. `projectRef` 判定：长度 >= 16 且只含 `[0-9a-zA-Z]` 视为 **project uuid**，否则视为 **项目标识（shortName / name）**。
3. 每个 `--bug` 参数的值形如 `PREFIX-NUMBER`；`PREFIX` 必须与"项目标识"一致（大小写不敏感）。若 `projectRef` 是 uuid，则跳过此校验，改为校验第 6.4 节解析出的 `projectID` 与该 issue 的 `project.id` 一致。
4. `--bug` 中出现不合 `^[A-Za-z][A-Za-z0-9_]*-[0-9]+$` 格式的值 → 退出码 2。

---

## 4. CLI 契约

```
python update_bug.py --bug FUCO-7 [FUCO-4 ...] [--leixing 安全问题] [--chengdu 致命]
                     [--lingling fusion2026.1.100.01] [--force] [--yes]
                     [--timeout 30] [--debug]
```

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `--bug` | nargs='+' | 是 | 一个或多个 issue key，如 `FUCO-7 FUCO-4` |
| `--leixing` | str | 否 | 缺陷类型，单选字段，值必须是合法选项 |
| `--chengdu` | str | 否 | 严重程度，单选字段，值必须是合法选项 |
| `--lingling` | str | 否 | `ling-build-version`，单选则校验选项，文本则只校验非空 |
| `--force` | flag | 否 | 强制重建缓存 `ones_field_cache.json`（含重新探测认证头） |
| `--yes` | flag | 否 | 真正执行修改；缺省为 DRY-RUN |
| `--timeout` | int | 否 | 单次 HTTP 超时秒数，默认 30 |
| `--debug` | flag | 否 | 打印请求方法/路径/状态码（不含 token 明文） |

参数校验（任一不满足 → 打印用法并退出码 2）：

- `--bug` 为空或为空列表。
- `--leixing`、`--chengdu`、`--lingling` 三者全部缺省（没有任何要改的属性）。
- `--bug` 中有重复值：自动去重并保留首次出现顺序，仅打印一行提示，不算错误。

环境变量：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `ONES_BUG` | 是 | ONES 里任意一个 issue 的页面 URL，用于提取 host / teamID / project |
| `ONES_API` | 是 | API key / token |
| `ONES_USER_ID` | 否 | 仅认证方式 3 需要 |

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 成功；或 DRY-RUN 正常结束；或无任何字段变化 |
| 1 | 校验失败（属性值不在候选列表、属性名定位不到、issue key 解析不到） |
| 2 | 参数或环境变量错误 |
| 3 | 网络 / 认证 / API 错误 |

---

## 5. 缓存文件 `ONES/ones_field_cache.json`

### 5.1 完整结构

```json
{
  "_meta": {
    "api_version": "v2",
    "host": "https://ones.cn",
    "team_id": "KKv1z9x9",
    "project_ref": "FUCO",
    "project_ref_kind": "short_name",
    "project_id": "6zAPyNY5xxxxxxxxxxxxxxxx",
    "auth_header": "Authorization: Bearer",
    "issue_key_supported": true,
    "generated_at": "2026-10-28T12:00:00+08:00",
    "script_version": "1.0.0"
  },
  "fields": {
    "leixing": {
      "field_id": "5f1f3b93xxxxxxxxxxxxxxxx",
      "name": "缺陷类型",
      "type_enum": 1,
      "select_mode": true,
      "options": [
        { "id": "5f1f3b93aaaa", "value": "安全问题" },
        { "id": "5f1f3b93bbbb", "value": "功能问题" }
      ]
    },
    "chengdu": {
      "field_id": "5f1f3b93cccccccccccccccc",
      "name": "严重程度",
      "type_enum": 1,
      "select_mode": true,
      "options": [
        { "id": "5f1f3b93dddd", "value": "致命" },
        { "id": "5f1f3b93eeee", "value": "严重" }
      ]
    },
    "lingling": {
      "field_id": "5f1f3b93ffffffffffffffff",
      "name": "ling-build-version",
      "type_enum": 1,
      "select_mode": true,
      "options": [
        { "id": "5f1f3b930000", "value": "fusion2026.1.100.01" }
      ]
    }
  },
  "issues": {
    "FUCO-7": { "id": "5f1f3b93issue0001", "number": 7, "project_id": "6zAPyNY5xxxxxxxxxxxxxxxx" }
  }
}
```

### 5.2 生成与读取规则

- 缓存不存在：自动构建（等价于一次局部 `--force`），无需用户额外操作。
- 缓存存在且未给 `--force`：只读缓存，不发属性列表请求；`issues` 段命中则不再翻页。
- 给了 `--force`：整份重建，`issues` 段清空。
- 缓存文件无法解析（JSON 损坏）：打印警告并按 `--force` 处理，重建后覆盖。
- 写盘时机：`_meta` 与 `fields` 构建成功后立即落盘；`issues` 每解析成功一个 key 就更新并落盘，避免中途失败重复翻页。
- 落盘路径固定为脚本同目录：`os.path.join(os.path.dirname(os.path.abspath(__file__)), "ones_field_cache.json")`，与当前工作目录无关。

---

## 6. 主流程

### 6.1 步骤

1. 读 `ONES_BUG`、`ONES_API`；缺失 → 打印缺失项 → 退出码 2。
2. 解析 URL（第 3 节），得到 `host`、`teamID`、`projectRef`、`issueKey`。
3. 载入或构建缓存（第 5 节、第 6.3 节）；首次或 `--force` 时执行第 2 节认证探测。
4. 校验三个属性值（第 6.5 节）。任一不合法立即退出码 1，不发任何写请求。
5. 解析每个 `--bug` 为 issue uuid（第 6.4 节）。
6. 逐个 issue `GET /project/issues/{issueID}?teamID=`，取出三个目标字段的当前值，计算 diff。
7. 无 `--yes`：按第 7 节格式打印 DRY-RUN 报告，退出码 0（即使全部字段无变化也返回 0）。
8. 有 `--yes`：对"存在实际变化"的 issue 逐个 `PUT`；单个失败不中断，记录错误后继续下一个。全部结束后打印汇总，失败数为 0 → 退出码 0，否则退出码 3。
9. 同一 issue 的多个字段合并为一次 `PUT`（`fieldValues` 数组放 1~3 个元素），不逐字段发请求。

### 6.2 字段定位总原则

- 名称匹配优先级：完全相等 → 去空白后相等 → 忽略大小写相等 → 命中 `aliases`。
- 匹配到 0 个：报 `FIELD_NOT_FOUND`，打印所有属性名与 fieldID，退出码 1。
- 匹配到多个：报 `FIELD_AMBIGUOUS`，打印候选，退出码 1。不做"猜一个"的兜底。
- 目标属性名固定为：`缺陷类型`、`严重程度`、`ling-build-version`（大小写不敏感，允许 `ling-build-version` 的变体 `ling-Build-Version`）。

### 6.3 构建 `fields` 段

1. `GET /project/issueFields?teamID={teamID}&limit=500`，按 `cursor` 翻页直到 `hasNextPage=false`。
2. 在返回的 `list` 中按 6.2 节定位三个属性，记录 `id → field_id`、`name`、`typeEnum → type_enum`、`options[] → options[{id, value}]`。
3. `select_mode` 判定：`type_enum == 1` 为 true（单选，值必须是选项之一）；`type_enum == 16` 记录为多选但本脚本按单选处理（只允许给一个值，`value` 传数组单元素）；其它类型 `select_mode = false`，只做非空校验。
4. `options[].value` 可能是对象而不是字符串：统一做 `str(value)` 归一化后再比较，同时保留原始结构用于提交。若原始结构是 `{"id":..,"value":..}` 形式，提交时优先使用 `id`。

### 6.4 解析 issue key → issue uuid

按 `_meta.issue_key_supported` 分支：

A. `true`：直接 `GET /project/issues/{issueKey}?teamID=`，取 `data.id`。
B. `false`：
   1. 先解析 `project_id`：`GET /project/projects?teamID=` 翻页（`limit=100`），按 `id == projectRef` 或 `name/短名 == projectRef`（大小写不敏感）匹配；匹配不到 → `PROJECT_NOT_FOUND`，退出码 1。
   2. 再翻页 `GET /project/issues?teamID=&projectID={project_id}&limit=100&cursor=`，按 `number == 目标编号` 匹配；接口若强制要求 `issueTypeID` 并返回参数错误，改为不带 `projectID` 的全量翻页 + 客户端按 `project.id` 与 `number` 过滤。
   3. 翻页上限 100 页；超出仍找不到 → `ISSUE_NOT_FOUND`，退出码 1。

解析结果写入 `issues` 段。命中缓存的 key 不再发起解析请求。

### 6.5 属性值校验

| 字段 | `select_mode=true` | `select_mode=false` |
| --- | --- | --- |
| 提交值 | 必须命中 `options[].value`（归一化后比较），提交 `option.id` | 原样提交字符串，只校验去空白后非空 |
| 未命中时 | 打印该字段全部合法值（每行一个，超过 40 个时折叠显示并提示已写入日志），退出码 1 | — |

补充规则：

- 用户传入值先做 `strip()`，再与选项比较；比较时忽略大小写与全半角差异（用 `unicodedata.normalize("NFKC", s)`）。
- 已经与当前值相同：视为"无变化"，不报错、不发请求（`--yes` 下也跳过）。
- 多个 issue 共用同一份校验结果，校验只做一次。

---

## 7. 输出格式

### 7.1 DRY-RUN

```
[ONES] host=https://ones.cn team=KKv1z9x9 project=FUCO(6zAPyNY5...) cache=hit
[ONES] 字段: 缺陷类型=5f1f3b93... (单选, 7 个选项) | 严重程度=5f1f3b93... (单选, 5 个选项) | ling-build-version=5f1f3b93... (单选, 12 个选项)
------------------------------------------------------------------------
FUCO-7  缺陷类型        : 功能问题 -> 安全问题   [变更]
FUCO-7  严重程度        : 一般 -> 致命           [变更]
FUCO-7  ling-build-version : fusion2026.1.100.01 -> fusion2026.1.100.01   [不变]
FUCO-4  缺陷类型        : 安全问题 -> 安全问题   [不变]
------------------------------------------------------------------------
DRY-RUN: 待修改 2 个 issue / 3 个字段；未做任何写操作。加 --yes 才会真正提交。
```

### 7.2 实际执行

```
FUCO-7  PUT 200 SUCCESS  已更新 2 个字段
FUCO-4  SKIP  无字段变化
------------------------------------------------------------------------
汇总: 成功 1 / 失败 0 / 跳过 1    耗时 1.4s
```

失败行形如：

```
FUCO-9  PUT 403 FAIL errorCode=Forbidden errorMsg=Scope is invalid
```

### 7.3 其它输出约束

- 对齐：`issue key` 左对齐占 10 列，字段名按中文字符宽度（`unicodedata.east_asian_width`）补空格。
- token 永不打印明文；`--debug` 下只打印 `token=****` 或前 4 位 + 后 4 位。
- 同时写入 `ONES/update_bug.log`（追加，UTF-8），记录时间、参数摘要、每个 issue 的旧值/新值/HTTP 状态码/错误体；日志中 token 同样脱敏。

---

## 8. 错误处理表

| 场景 | 行为 | 退出码 |
| --- | --- | --- |
| `ONES_BUG` / `ONES_API` 缺失 | 打印缺失变量名与设置示例 | 2 |
| `ONES_BUG` 无法解析出 host/team/project | 打印 URL 与期望格式 | 2 |
| `--bug` 格式非法 / 与项目前缀不一致 | 打印非法项与期望前缀 | 2 |
| 三个属性全未提供 | 打印用法 | 2 |
| 属性值不在选项列表 | 打印该字段全部合法值 | 1 |
| 属性名在 team 内定位不到 / 多义 | 打印全部属性名+fieldID | 1 |
| issue key 解析不到 / 项目解析不到 | 打印已翻页数量与最后 cursor | 1 |
| HTTP 401 | 提示 token 无效或过期，建议 `--force` 重探 | 3 |
| HTTP 403 | 提示缺少 scope（如 `read:project:issueField`、`write:project:issue`） | 3 |
| HTTP 404（接口路径） | 提示 host 或 openapi 前缀不对，`ENDPOINT_NOT_FOUND` | 3 |
| HTTP 429 / 5xx / 超时 | 指数退避重试 3 次（1s/2s/4s），仍失败则记为失败项 | 3（当且仅当有失败项） |
| 响应 `result=FAIL` | 解析 `errorCode`/`errorMsg` 原样打印，记为失败项，不重试 | 3 |
| 缓存 JSON 损坏 | 警告并重建缓存，不中断 | 0/1/3 视后续结果 |

重试与幂等说明：`PUT /project/issues/{issueID}` 是幂等写，重试安全；但仅在网络层错误（连接重置、超时、429、5xx）时重试，业务错误（`result=FAIL`）不重试。

---

## 9. 实现结构（单文件）

文件：`ONES/update_bug.py`，不使用任何自有模块 import。

依赖：`requests`（用户已确认可用）+ 标准库 `argparse/os/sys/json/re/time/datetime/unicodedata/shutil`。`import requests` 失败时打印 `pip install requests` 并退出码 2（用 `try/except ImportError` 包裹，不把异常堆栈抛给用户）。

| 函数 | 职责 |
| --- | --- |
| `parse_args()` | 定义并解析 CLI，返回 `argparse.Namespace` |
| `load_env()` | 读 `ONES_BUG`/`ONES_API`/`ONES_USER_ID`，缺失即退出码 2 |
| `parse_ones_url(url)` | 按第 3 节返回 `{host, team_id, project_ref, project_ref_kind, issue_key}` |
| `mask(secret)` | token 脱敏 |
| `ApiClient(host, token, user_id, timeout, debug)` | 统一封装 `get/post/put`：拼 `{host}/openapi/v2`、注入认证头（含第 2 节探测结果）、重试 3 次、解析 `result=FAIL`、写日志 |
| `ApiClient.probe_auth(team_id)` | 第 2 节认证探测，返回 `auth_header` 字符串 |
| `ApiClient.probe_issue_key(team_id, issue_key)` | 2.3 节，返回 `bool` |
| `cache_path()` | 返回脚本同目录下的 `ones_field_cache.json` 绝对路径 |
| `load_cache()` / `save_cache(cache)` | 读写缓存，损坏时返回 `None` |
| `fetch_all_issue_fields(client, team_id)` | 翻页拿全部属性 |
| `pick_field(field_list, names)` | 6.2 节名称匹配，返回唯一属性或抛出 `FieldError` |
| `build_cache(client, meta)` | 组装 `_meta` 与 `fields` 段并落盘 |
| `resolve_project_id(client, team_id, project_ref, kind)` | 6.4 B.1 |
| `resolve_issue_id(client, cache, key)` | 6.4 全流程，命中 `issues` 缓存则直接返回 |
| `normalize(s)` | `NFKC` + `strip` + `casefold` |
| `validate_value(field, raw)` | 6.5 节，返回 `(type, value_to_submit, label)` |
| `get_current(client, team_id, issue_id)` | 详情接口，返回 `{field_id: (raw_value, label)}` |
| `build_payload(field_meta, submit_value)` | 组装 `fieldValues` 数组 |
| `apply_update(client, team_id, issue_id, payload)` | 单 issue `PUT` |
| `print_report(rows)` | 第 7 节格式化输出与中文宽度对齐 |
| `main()` | 第 6.1 节 9 步编排，统一捕获异常并映射退出码 |
| `FieldError` / `ApiError` / `UsageError` | 自定义异常，`main()` 中映射到 1/3/2 |

实现约束：

- 所有 `sys.exit` 只在 `main()` 与参数校验路径出现；其余函数抛异常。
- 不在脚本内硬编码 host、teamID、字段 ID、选项，全部来自 `ONES_BUG` 与缓存。
- 不使用 `print()` 之外的日志库；`--debug` 通过 `ApiClient.debug` 开关控制。
- 代码注释用中文，函数级注释说明"做什么 + 输入 + 返回"，不写行号、不写 TODO。

---

## 10. 验收用例

| 编号 | 命令 | 期望 |
| --- | --- | --- |
| A1 | `python update_bug.py --help` | 打印全部参数说明（含 `--force`/`--yes` 语义），退出码 0 |
| A2 | 清空 `ONES_API` 后 `python update_bug.py --bug FUCO-7 --leixing 安全问题` | 打印 `ONES_API 未设置` 与设置示例，退出码 2 |
| A3 | `python update_bug.py --bug FUCO-7` | 打印"至少提供一个要修改的属性"，退出码 2 |
| A4 | `python update_bug.py --bug FUCO-7 --leixing 不存在的类型` | 打印"缺陷类型"全部合法值，退出码 1，无任何 PUT 请求 |
| A5 | `python update_bug.py --bug FUCO-7 --leixing 安全问题` | 生成 `ones_field_cache.json`（首次），打印 DRY-RUN diff，退出码 0 |
| A6 | `python update_bug.py --bug FUCO-7 --leixing 安全问题 --force` | 重建缓存（`_meta.generated_at` 变化、`issues` 清空后再解析），DRY-RUN 输出同上，退出码 0 |
| A7 | `python update_bug.py --bug FUCO-7 FUCO-4 --leixing 安全问题 --chengdu 致命 --lingling fusion2026.1.100.01 --yes` | 每个 issue 一次 PUT，打印每个字段的旧值→新值，末尾汇总，退出码 0 |
| A8 | 重复执行 A7 | 全部 `[不变]`，不发 PUT，退出码 0 |
| A9 | 把 `ONES_API` 改成一个无效值并加 `--force` | `AUTH_FAILED`，打印响应体，退出码 3 |
| A10 | 手工破坏 `ones_field_cache.json`（写入非法 JSON）后执行 A5 | 打印缓存损坏警告并自动重建，最终正常 DRY-RUN，退出码 0 |

执行顺序：A1→A4（离线可验）→ A5/A6（只读）→ A7（需人工确认目标 issue 可用）→ A8→A10。
A7 属真实写操作，执行前必须由用户确认目标 issue 编号与目标值。

---

## 11. 明确不做

- 不修改 `aiai.md`。
- 不把代码拆成多个文件，不 import 自有模块。
- 不实现批量跨项目修改（一次运行只处理 `ONES_BUG` 指向的同一个项目）。
- 不实现自动回滚；失败项只记录，由人工复核。
- 不删除任何生成物（`ones_field_cache.json`、`update_bug.log`、本计划文档）。
- 不写入与任务无关的文件。
- 不做并发请求（串行足够，且避免触发限流）。
