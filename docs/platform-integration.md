# AD-Creativity 集成与发布

平台保留用户鉴权、资产下载、翻译工作区、任务持久化及 TLS 生命周期。原 `backend.app.services.video_subtitle_grounding` 路径成为配置和日志注入层，实际提取逻辑由本包执行。平台传入 prepared video 时不需要额外网络传输。

首版使用固定 wheel 作为部署依赖，既不依赖 GitHub 登录态，也不要求生产服务器存在本地 standalone 目录：

```text
requirements.txt
└── ./vendor/video_subtitle_grounding-0.1.0-py3-none-any.whl
```

## 更新提取包

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

本地仓库初始化不复制原平台历史。建议名称 `video-subtitle-grounding`，可见性 private。提交前检查仅包含 README、配置示例、src、合成测试和构建定义。不要提交 `.env`、运行日志、用户视频或调试模型原文。
