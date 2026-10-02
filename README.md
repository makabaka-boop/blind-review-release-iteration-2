# 同行评审系统（Peer Review）

基于 **React + FastAPI + PostgreSQL** 的双角色同行评审系统，使用 Docker Compose 一键运行，
并内置一次性验收（verify）测试服务。

## 角色

| 角色 | 能力 |
| --- | --- |
| **管理员 admin** | 创建评审轮次（≤20 份稿件、≤10 名评审员）、创建评审员账号、手动分配稿件、登记/删除利益冲突、查看进度、处理评审员**回避申请**（驳回恢复草稿 / 批准并指定接替者）、在**全部**有效分配最终提交且**无待处理回避**后**原子冻结**整轮 |
| **评审员 reviewer** | 只能看到本轮的**匿名稿号/标题**和**自己的分配**；按修订号保存草稿、最终提交；发现潜在利益冲突时对**尚未最终提交**的指派申请回避；冻结前看不到任何作者身份 |

默认管理员：`admin` / `adminpass`（可用 `ADMIN_USER` / `ADMIN_PASSWORD` 环境变量覆盖）。

## 快速开始

```bash
docker compose up --build
# 前端 http://localhost:8080 ，API http://localhost:8000 （/api/health 健康检查）
```

## 验收（固定流程）

```bash
docker compose config --quiet
docker compose build
docker compose run --rm verify
```

`verify` 是一次性容器（profile `test`，不随 `up` 启动），等待 API 健康后执行
`verify/run_verify.py` 中的 135 项断言并退出；全部通过时退出码为 0。

## 关键业务规则与实现

- **名额上限**：每轮至多 20 份稿件、10 名评审员；每名评审员至多 3 份分配，超限请求返回 400。
- **利益冲突**：管理员可维护「稿件 × 评审员」冲突关系。
  - 有冲突的分配无法创建（400）；
  - 已存在分配的冲突无法再登记（400）；
  - 冻结后冲突关系不可改（409）。
- **匿名性（冻结前）**：评审员接口的列表、详情、错误响应一律不含 `author`：
  - 轮次/稿件列表只给 `id, number, title`；
  - 稿件详情仅对**本人被分配**的稿件开放，未分配/他人/不存在统一返回 404
    （不区分存在性，不回显任何作者信息）；
  - 他人的分配不可见，草稿/提交同样返回 404；
  - 未加入本轮的评审员访问该轮返回 404；
  - 所有 API 响应带 `Cache-Control: no-store`，`index.html` 也禁止缓存，
    防止冻结后揭示的作者身份经共享浏览器/代理缓存泄露；
  - 服务端 500 只返回通用文案，异常详情仅写日志。
- **草稿与修订号（乐观并发控制）**：
  - `PUT /api/reviewer/assignments/{id}/draft` 必须携带 `expected_revision`；
  - 两个并发同修订号保存，数据库行锁串行化：一个成功（`revision+1`），
    另一个得到 `409 {current_revision}`，内容不会互相覆盖；
  - `POST .../submit` 为最终提交；并发重复提交同样恰好一个成功、一个 409。
- **原子冻结与提交/冻结竞态**：
  - 冻结时先对 `rounds` 行加 `FOR UPDATE` 锁，并校验所有分配均为 `submitted`，
    然后在**同一事务**内把轮次置为 `frozen`；
  - 评审提交/存草稿也会对同一 `rounds` 行加锁，与冻结形成确定的先后顺序：
    提交先拿锁 → 提交生效，随后冻结可成功；冻结先拿锁 → 提交收到 409；
    不会出现“冻结后仍写入”或“状态不一致”。
- **冻结后不可变**：存草稿、提交、增删稿件/评审员/冲突/分配均返回 409；
  已提交评审内容保持不变。冻结（揭示作者）后，评审员接口的稿件字段中出现 `author`。
- **回避（recusal）流程**：
  - 评审员仅能对**自己的、尚未最终提交**的指派发起
    `POST /api/reviewer/assignments/{id}/recusal`（理由不能为空）；
    申请后指派为 `recusal_pending`，**暂停保存草稿与终审**，重复申请返回 409。
  - 管理员可 `POST /api/admin/recusals/{id}/reject` **驳回**：指派恢复为可编辑
    `draft`，原草稿内容与修订号原样保留；
    或 `POST /api/admin/recusals/{id}/approve` **批准**，且必须在**同一事务**内完成：
    旧指派置为 `recused`（不可再提交、不可删除的历史记录）→ 登记「稿件 × 旧评审员」
    冲突 → 向接替评审员发放一份**全新空白指派**（revision 0、空内容）。
  - 接替者必须**本轮在列、与该稿件无冲突、未满 3 份名额、且不是本人**；
    任一校验失败整体回滚，旧指派保持 `recusal_pending`，名额不变。
  - **隔离**：`recused` 历史不出现在接替者接口或任何匿名页面；旧评审员对其
    GET / 存草稿 / 提交 / 再回避一律 404（与不存在不可区分）；旧草稿文本与作者
    身份均不会经接替者接口泄露。旧指派不再占用名额，仅管理员详情可见。
  - **屏障与并发**：存在待处理回避时整轮**不能冻结**（409）；回避申请、保存、
    终审、管理员批准/驳回、转派、冻结全部沿现有 `rounds` 行 `FOR UPDATE` 锁排队，
    形成确定顺序——同一事务内任何校验失败都保持旧指派与名额不变；两个并发批准
    恰好一个成功，另一个 404，且只产生一份接替指派。

## 架构

```
services:
  db      postgres:16-alpine（健康检查 pg_isready）
  api     FastAPI + psycopg2，启动时建表并播种管理员（健康检查 /api/health）
  web     Vite 构建 React，nginx 托管静态文件并把 /api 反代到 api
  verify  一次性 Python 验收脚本（profile: test，depends_on api healthy）
```

### 主要接口

- `POST /api/login`
- 管理员（需 admin token）：`/api/admin/rounds`、`/api/admin/reviewers`、
  `/api/admin/rounds/{id}/{manuscripts,reviewers,conflicts,assignments,freeze}`、
  `/api/admin/recusals/{id}/{reject,approve}`
- 评审员（需 reviewer token）：`/api/reviewer/rounds`、
  `/api/reviewer/rounds/{id}/manuscripts/{mid}`、
  `/api/reviewer/assignments/{id}`、`.../draft`、`.../submit`、`.../recusal`

角色越权：评审员访问管理员接口 → 403；管理员访问评审员接口 → 403；
无/伪造令牌 → 401。

## 本地开发

```bash
# 后端
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload

# 前端
cd frontend && npm install && npm run dev   # /api 已配置代理到 :8000
```

需要可用的 PostgreSQL（默认 DSN `postgresql://review:review@db:5432/review`，
可用环境变量 `DATABASE_URL` 覆盖）。
