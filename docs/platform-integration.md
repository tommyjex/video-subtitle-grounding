# AD-Creativity 集成与发布

平台保留用户鉴权、资产下载、翻译工作区、任务持久化及 TLS 生命周期。提取和翻译路径成为配置和日志注入层；排版和 MediaKit 多轨客户端也由本包提供。平台传入 prepared video 时不需要额外网络传输。平台渲染服务继续负责供应商任务持久化、恢复轮询、资产签名和成片入库。

当前使用固定 wheel 作为部署依赖，既不依赖 GitHub 登录态，也不要求生产服务器存在本地 standalone 目录：

```text
requirements.txt
└── ./vendor/video_subtitle_grounding-0.3.1-py3-none-any.whl
```

## 更新字幕包

1. 在独立仓库修改代码、提升版本，运行离线测试。并行开发时，必须先确认核心实现、版本与角色规则测试完成，再构建最终发布产物：

   ```bash
   python -m pip install '.[dev]'
   python -m pytest
   ruff check src tests
   python -m build
   ```

   若已有隔离构建环境，可使用 `/tmp/ad-subtitle-split-build/isolated-env/bin/python -m build --no-isolation`。

2. 检查 wheel 与 sdist 内容及版本，确认包含产品识别与混合字体实现、不含密钥、运行日志或用户数据。将新版 wheel 复制到平台 `vendor/`，保留旧 wheel 供同步回滚；更新 `requirements.txt` 的精确文件名与 `vendor/README.md` 的 SHA256。按既有导出约定将 sdist 复制为 `exports/video-subtitle-grounding-0.3.1-source.tar.gz`；分别核对 wheel 和源码归档的内容与 SHA256。
3. 在隔离环境及平台根目录 `.venv` 安装 wheel（已有依赖时使用 `--no-deps`），从源码目录外核对实际导入位置、包版本、排版版本与角色选择；字体使用 mock，不调用计费接口。在平台运行字幕提取、翻译、排版、压制和多轨集成测试：

   ```bash
   python -m pip install -r requirements.txt
   PYTHONPATH=. python -m pytest backend/tests/test_video_subtitle_grounding_service.py \
     backend/tests/test_subtitle_translation_execution.py
   ```

4. 按平台原部署说明和数据库发布门禁发布。此次拆分本身没有数据库结构变更；平台启动仍执行原有数据库初始化。

后续有私有包索引或经过批准的 GitHub release 时，可替换为固定版本包或固定 commit 的 Git 安装地址。不要将 GitHub Token 写入 requirements、仓库 URL 或 shell 输出。

## 回滚

新 API 和旧平台接口均保留当前字段契约。首版迁移需要同时发布兼容层与 wheel；只发布兼容层会导致 import 失败。回滚应同步恢复平台兼容层和依赖版本。

0.2.2 回滚到 0.2.1 时还应恢复对应的排版版本与前端默认字体规则，不能只替换 wheel。保留历史成片和旧任务，不执行全库更新。

## 0.3.1 产品字体与自动判定集成

平台固定 0.3.1 wheel，排版版本 `single-line-adaptive-panel35-product-font-v3`，字典版本 `tineco-names-20261116-v1`。平台注入私有图层存储，工作区只保留图层 hash 和同源鉴权 URL；提交时刷新 TOS 签名 URL。内容地址对象保留供运行中的和历史任务读取，不在编辑时删除旧图层，也不进入公共资产画廊。

新增字段在既有工作区/任务 JSON 中保存，不需要 DDL 迁移。发布时同时更新平台 schema/service/routes、前端和 wheel。线上仍只拉平台仓库，运行 `scripts/deploy_server.sh` 安装 wheel、构建前端并重启服务，不拉独立仓库；脚本不会执行 git pull。新翻译自动识别名称，旧译文或编辑后的名称在保存和 POST 排版计算时自动判定，前端压制前自动保存。判定指纹包含文本、同块上下文和字典版本，重复操作复用结果；失败自动重试一次，仍失败保留普通字幕，产品判定不提示人工校验。GET 读取不调用模型。重新压制使用新版本任务指纹，历史成片不自动变更。

回滚到 0.2.2 需同步回滚平台适配层、前端、依赖及排版/任务指纹版本；旧包没有产品图层模块，不能只换 wheel。真实模型和 MediaKit 尚未计费验收时应明确标注这一限制。

## 0.2.2 角色字体与排版版本

`LAYOUT_VERSION` 为 `single-line-adaptive-panel35-role-font-v2`。仅自动模式改变：`style.font` 未设置、为 `null` 或为空时，`headline` 使用 `CenturyGothicBold`，`body`、`footnote`、空值、缺失及未知角色使用 `CenturyGothic`（Regular，不是 Light/Thin）。逐条按当前角色选择，同一 `block_id` 中可有不同字体；不按行号、大小写或内容猜测。

Bold 和 Regular 使用各自的实际字体文件进行测量与 MediaKit 提交，文件字体保持 `bold: false`。自动缺字回退保留 `ALi_PuHui` 和未验证警告，标题请求合成加粗，其余角色不加粗。人工字体资源 ID 或合法 URL 覆盖保持优先，清空后恢复自动；旧 `font_type` 摘要不能当作人工选择，重新计算必须更新字体与度量。

平台默认字体提示与校对预览采用同一规则。新排版版本使相同草稿首次按新规则压制时创建新任务，同一新版本重复提交继续使用既有幂等机制；不重跑提取或翻译，不修改原始 bbox、时间、角色、译文，不自动重写已有成片，也没有数据库迁移。

离线验收需覆盖角色选择、缺失及未知角色、同块不同角色、人工覆盖、缺字回退、单行测量和版本幂等。字体文件可读取及离线测试不代表真实 MediaKit 字重和成片质量验收；真实压制是否执行及结果应单独记录。

## 仓库发布

仓库已发布为 https://github.com/tommyjex/video-subtitle-grounding ，可见性 private。本地仓库初始化不复制原平台历史。提交前检查仅包含文档、配置示例、src、离线测试/OFL 字体和构建定义。不要提交 `.env`、运行日志、用户视频或调试模型原文。

0.2.0 需同时部署以下平台适配层：`modelark_subtitle_translation.py`、`subtitle_translation_layout.py` 和 `mediakit_multitrack.py`。多轨编辑器也复用此客户端，因此发布前同时运行平台多轨测试。

0.2.1 平台提取任务传入异步 `on_file_uploaded(file_id)`，在上传成功后立即将 ID 和时间写入该任务的 `input_snapshot.provider_file_id` 与 `provider_file_uploaded_at`，并向 TLS 记录 `subtitle_translation.file_uploaded` 事件（`task_id`、`workspace_id`、`provider_file_id`）。保存使用最新任务快照，完成/失败/重启保留关联；重试任务记录新的文件 ID。回调持久化失败时停止模型请求并清理供应商文件。没有数据库迁移。

按任务查询关联：

```sql
SELECT id AS task_id,
       JSON_UNQUOTE(JSON_EXTRACT(input_snapshot, '$.provider_file_id')) AS file_id,
       JSON_UNQUOTE(JSON_EXTRACT(input_snapshot, '$.provider_file_uploaded_at')) AS uploaded_at
FROM tool_tasks
WHERE type = 'video_subtitle_translation'
  AND JSON_EXTRACT(input_snapshot, '$.provider_file_id') IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;
```

上传时间采用带 UTC 偏移的 ISO 8601 格式，展示时转换为北京时间。历史任务不会补造关联，文件 ID 也不代表供应商文件仍然存在。
