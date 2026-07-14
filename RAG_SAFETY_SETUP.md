# RAG 与三层安全机制运行说明

## 1. 配置

复制 `.env.example` 中需要的配置到项目已有 `.env`。BGE-M3 通过兼容 OpenAI Embeddings 的远程 API 调用，本项目不会下载模型权重。

必须配置：

- `OPENAI_API_KEY`：现有需求提取、护栏分类和 RAG 回答使用。
- `BGE_M3_API_URL`、`BGE_M3_API_KEY`：生成查询和知识 Chunk 的向量。
- `QDRANT_URL`：Qdrant 服务地址。
- `ADMIN_TOKEN`：访问 `/admin` 使用。

飞书审核为可选能力。配置 `GOHUMANLOOP_API_URL` 和 `GOHUMANLOOP_API_KEY` 后，敏感计划会通过 GoHumanLoop API Provider 发送到飞书；未配置时计划仍会进入本地管理员后台等待审核。

## 2. 建立知识库

确认 Qdrant 已启动且 BGE-M3 API 配置有效，然后运行：

```powershell
python scripts/index_kb.py --recreate
```

脚本只扫描 `kb/**/*.md`，将文档切块后调用远程 BGE-M3 API，再写入 `chinatravel_safety_knowledge` Collection。

## 3. 启动和审核

```powershell
python run_web.py
```

- 用户页面：`http://127.0.0.1:8000/`
- 管理员页面：`http://127.0.0.1:8000/admin`

管理员在页面输入 `ADMIN_TOKEN`。拒绝敏感计划时必须填写原因，该原因会显示在用户聊天界面。

## 4. 安全边界

- 输入先经过越狱检查和四分类，不相关输入不会进入 RAG 或规划。
- RAG 最高相似度低于 `RAG_SCORE_THRESHOLD` 时不会调用回答模型。
- 包含儿童、未成年人或老人的旅行计划在审核通过前不会通过会话接口发布。
- 飞书或本地后台遵循第一次有效审核决定生效。
