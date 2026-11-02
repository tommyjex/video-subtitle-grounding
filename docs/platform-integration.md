# AD-Creativity 集成与发布

平台保留用户鉴权、资产下载、翻译工作区、任务持久化及 TLS 生命周期。提取和翻译路径成为配置和日志注入层；排版和 MediaKit 多轨客户端也由本包提供。平台传入 prepared video 时不需要额外网络传输。平台渲染服务继续负责供应商任务持久化、恢复轮询、资产签名和成片入库。

当前使用固定 wheel 作为部署依赖，既不依赖 GitHub 登录态，也不要求生产服务器存在本地 standalone 目录：

```text
requirements.txt
└── ./vendor/video_subtitle_grounding-0.2.0-py3-none-any.whl
```

## 更新字幕包

1. 在独立仓库修改代码、提升版本，运行离线测试并构建：

   ```bash
   python -m pip install '.[dev]'
   python -m pytest
   ruff check src tests
   python -m build
   ```

2. 将构建的新版 wheel 复制到平台 `vendor/`，更新 `requirements.txt` 的精确文件名；核对 SHA256。
3. 在平台运行字幕提取与翻译集成测试：

   ```bash
   python -m pip install -r requirements.txt
   PYTHONPATH=. python -m pytest backend/tests/test_video_subtitle_grounding_service.py \
     backend/tests/test_subtitle_translation_execution.py
   ```

4. 按平台原部署说明和数据库发布门禁发布。此次拆分本身没有数据库结构变更；平台启动仍执行原有数据库初始化。

后续有私有包索引或经过批准的 GitHub release 时，可替换为固定版本包或固定 commit 的 Git 安装地址。不要将 GitHub Token 写入 requirements、仓库 URL 或 shell 输出。

## 回滚

新 API 和旧平台接口均保留当前字段契约。首版迁移需要同时发布兼容层与 wheel；只发布兼容层会导致 import 失败。回滚应同步恢复平台兼容层和依赖版本。

## 仓库发布

仓库已发布为 https://github.com/tommyjex/video-subtitle-grounding ，可见性 private。本地仓库初始化不复制原平台历史。提交前检查仅包含文档、配置示例、src、离线测试/OFL 字体和构建定义。不要提交 `.env`、运行日志、用户视频或调试模型原文。

0.2.0 需同时部署以下平台适配层：`modelark_subtitle_translation.py`、`subtitle_translation_layout.py` 和 `mediakit_multitrack.py`。多轨编辑器也复用此客户端，因此发布前同时运行平台多轨测试。
