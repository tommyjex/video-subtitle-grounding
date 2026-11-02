# Video subtitles — 提取、翻译与压制

基于火山方舟 Ark 和 MediaKit 的独立视频字幕后端。扫描整帧文字，提取时间与位置，按视觉行翻译，再将译文压制到无字幕视频。当前版本 **0.2.0**，仓库与 Python 包名保持 `video-subtitle-grounding`。它不依赖 AD-Creativity 的数据库、资产库或前端。

## 已有能力

- 多行按视觉行拆分；保留 `block_id` 和 `line_index`。
- 输出 0–999 归一化 bbox 和视频展示像素 bbox；处理旋转视频。
- Ark Files 上传、处理状态等待、Responses 调用和 best-effort 文件清理，支持大于 50MB 的视频。
- 默认模型 `doubao-seed-evolving`，默认抽帧 2 FPS、`max_output_tokens=16384`、thinking disabled。
- 保留现有的容错校验：无效样式回退，部分有效字幕保留；完全无效的模型结果返回结构化错误。
- HTTPS 视频下载、重定向校验、公网地址检查、512MiB 下载上限、10 分钟视频上限和临时文件清理。
- 聚合 JSON 日志和可选 TLS 投递。模型原文默认不记录，显式 debug 时仅发送 TLS。
- Ark 按字幕 ID 翻译，提供上下文、批次预算、截断恢复和失败 ID 重试。
- 字幕排版保持逐行独立、纵向范围固定，先横向扩框再缩字号；保留字体测量、缺字回退与颜色策略。
- MediaKit 多轨压制提交、任务查询和轮询，保留稳定的幂等 token、原视频音轨和展示尺寸。

当前域名检查与 HTTP 连接各自解析 DNS，尚未实现连接目标 IP 固定；部署端仍应限制对内网及云 metadata 地址的出站访问。本次拆分保留已有下载行为。

## 安装与 SDK 使用

需要 Python 3.11+；URL 输入需要系统安装 FFprobe（通常由 FFmpeg 提供）。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install .
export ARK_API_KEY='你的方舟 API Key'
```

```python
import asyncio
from video_subtitle_grounding import (
    Settings, VideoSubtitleGroundingRequest, VideoSubtitleGroundingService,
)

async def main():
    settings = Settings.from_environment()
    service = VideoSubtitleGroundingService(settings=settings)
    try:
        result = await service.run(VideoSubtitleGroundingRequest(
            video_url="https://your-media-host/video.mp4",
            sampling_fps=2,
        ))
        # result.model_dump(mode="json") 交给业务端保存。
        # 字幕内容和带签名 URL 不要写入本地日志。
        return result
    finally:
        await asyncio.to_thread(service.provider.client.close)

result = asyncio.run(main())
```

对于业务端已下载的视频，可以构造 `media.PreparedRemoteVideo` 调用 `service.extract(prepared)`；文件仍由调用方清理。可通过 `provider`、`preparer`、`event_logger` 和 `tls_only_logger` 注入现有运行时。

`Settings()` 不加载环境；`Settings.from_environment()` 从进程环境加载。包不会自动寻找父目录 `.env`，因此不会读取其他项目的生产数据库或密钥。

## 独立 FastAPI 服务

```bash
python -m pip install '.[server]'
cp .env.example .env
# 填写 ARK_API_KEY 和独立随机 SERVICE_API_KEY；可配置 TLS。
uvicorn video_subtitle_grounding.api:create_app \
  --factory --env-file .env --host 127.0.0.1 --port 8010 --no-access-log
```

`SERVICE_API_KEY` 建议使用至少 32 字节随机值。它仅用于后端调用，不能嵌入浏览器前端。服务生产部署在 HTTPS 反向代理或私网之后。原平台的用户登录和权限仍由原平台处理。

- `GET /health`：服务存活检查，不验证实际模型可用性。
- `POST /v1/subtitles/extract`：`X-API-Key` 鉴权。
- `POST /v1/subtitles/translate`：按字幕 ID 翻译，`X-API-Key` 鉴权。
- `POST /v1/subtitles/render`：编译字幕并异步提交压制，返回 `task`、`layouts`、`warnings`。
- `GET /v1/subtitles/render/{task_id}`：查询供应商任务，完成后返回 `output_video_url`。
- `GET /docs`：OpenAPI 文档。

```bash
curl http://127.0.0.1:8010/v1/subtitles/extract \
  -H "X-API-Key: ${SERVICE_API_KEY}" \
  -H 'Content-Type: application/json' \
  --data '{"video_url":"https://your-media-host/video.mp4","sampling_fps":2}'
```

返回结构保持原提取接口契约：

```json
{
  "model": "doubao-seed-evolving",
  "video": {"width": 1920, "height": 1080, "duration_ms": 30000, "rotation": 0},
  "subtitles": [],
  "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
  "latency_ms": 1000
}
```

这是单次同步提取接口，没有持久化任务队列。默认每个进程同时接受 1 个提取，容量不足返回 429；客户端不要自动重试仍在执行的请求。HTTP 代理及客户端超时应覆盖下载、Files 处理、模型调用和清理的时间预算。多 worker 会分别拥有自己的并发额度。

无效 API Key 返回 401，输入错误返回 422，下载或供应商错误返回 502；错误包含 `code`、`phase`、`message`。API 不返回 provider 原文。请求的 `sampling_fps` 默认 2，允许 0.5–5。

## TLS 和隐私

开启 `TLS_ENABLED=true` 后填写 endpoint、project、topic 和 AK/SK，服务启动时校验配置完整性。日志以北京时间展示，仅包含数量、阶段、耗时、Token 等元数据。供应商 SDK 自身的 HTTP debug 日志不要启用。

`ENABLE_EXPERIMENTAL_VIDEO_SUBTITLE_GROUNDING_DEBUG=true` 需要同时启用 TLS。此开关只在模型输出校验失败时将原文写入 TLS；原文不会进入 stdout。SDK 默认丢弃原文 debug 事件，嵌入应用如需记录必须注入 TLS-only 回调。凭据通过运行时环境提供，不进入 Git 或容器镜像。

翻译适配器保留平台的 TLS-only 原文回调契约；独立 SDK 默认丢弃原文事件，独立 HTTP 服务仅在 TLS 已配置时投递该事件。提取 debug 开关不控制翻译回调。

## Docker

```bash
docker build -t video-subtitle-grounding:0.2.0 .
docker run --rm --env-file .env -p 127.0.0.1:8010:8010 \
  video-subtitle-grounding:0.2.0
```

容器采用非 root 用户，包含 FFprobe。本次拆分未执行 Docker 镜像构建或生产部署。

## 测试与发版

```bash
python -m pip install '.[dev]'
python -m pytest
ruff check src tests
python -m build
```

所有默认测试使用离线 provider 和 HTTP fixture，不访问业务数据库，不执行付费模型调用。排版测试使用 OFL 字体与真实 FreeType 测量。测试证明契约、清理、鉴权和兼容性，不代替真实 OCR、翻译和成片渲染质量验收。

修改行为或接口时提升 `pyproject.toml` 和 `__init__.py` 版本；API 版本引用包版本。消费者安装固定版本。当前平台使用 `video_subtitle_grounding-0.2.0-py3-none-any.whl`，发布步骤见 `docs/platform-integration.md`。

## 来源与边界

由 AD-Creativity-Platform 的字幕提取、翻译、排版及 MediaKit 多轨客户端拆出，保留原调用契约和默认行为。仓库不含平台工作区、用户鉴权、数据库、前端、生产 `.env`、字幕导出或用户视频。压制输出仍由调用方下载并存储。尚未指定开源许可证，默认按私有仓库维护；测试字体许可证见 `tests/fixtures/subtitle_fonts/OFL.txt`。

## 翻译与压制用法

翻译使用 `ARK_API_KEY`；压制需额外配置 `MEDIAKIT_API_KEY`。提取和翻译可在未配置 MediaKit 时使用，压制接口返回 503。翻译请求如下，省略 `segment_ids` 时翻译全部记录；失败批次保留已成功结果并返回 `failed_ids`：

```json
{
  "segments": [
    {"segment_id": "line-1", "text": "Powerful cleaning", "block_id": "block-1", "line_index": 0}
  ],
  "source_language": "en",
  "target_language": "de",
  "segment_ids": ["line-1"]
}
```

Python SDK 可以直接接收提取结果转换得到的字典，核心模块可单独调用：

```python
from video_subtitle_grounding import (
    Settings, ModelArkSubtitleTranslationAdapter, SubtitleRenderService,
)

async def translate_and_submit(segments, clean_video, idempotency_key):
    settings = Settings.from_environment()
    translator = ModelArkSubtitleTranslationAdapter(settings)
    try:
        result = await translator.translate(
            segments, segment_ids=[s["segment_id"] for s in segments],
            source_language="auto", target_language="de",
        )
    finally:
        await asyncio.to_thread(translator.client.close)
    if result["failed_ids"]:
        return result  # 调用方补译或审核后再压制。
    reviewed = [
        {**s, "translated_text": result["translations"][s["segment_id"]]}
        for s in segments
    ]
    renderer = SubtitleRenderService(settings)
    submitted = await renderer.submit(
        reviewed, clean_video, idempotency_key=idempotency_key,
    )
    # 保存 task_id 后即可通过 get_task 或 poll 恢复查询。
    return submitted
```

以上示例复用前文的 `import asyncio`。压制 HTTP 请求结构：

```json
{
  "idempotency_key": "my-video-draft-revision-1",
  "video": {
    "source_url": "https://your-media-host/clean-video.mp4",
    "width": 1920,
    "height": 1080,
    "duration_ms": 30000,
    "rotation": 0,
    "fps": 30,
    "has_audio": true,
    "target_language": "de"
  },
  "segments": [{
    "segment_id": "line-1",
    "text": "Powerful cleaning",
    "translated_text": "Leistungsstarke Reinigung",
    "block_id": "block-1",
    "line_index": 0,
    "start_ms": 1000,
    "end_ms": 3000,
    "bbox_normalized": [100, 800, 900, 900],
    "alignment": "center",
    "background_color": "#333333",
    "style": {}
  }]
}
```

`width`/`height` 必须是展示方向尺寸，bbox 为 0–999 坐标。输入应为时间轴对齐的无字幕视频；压制只叠加译文，不移除原字幕。字体仅下载服务端登记的 URL；需要网络访问字体资源和 MediaKit。默认小字/复杂背景策略保持原平台实现，包括 `#33333359` 底板。

客户端保存 `task.task_id`、布局和警告，定期调用查询接口；SDK 也可 `await renderer.poll(task_id=...)`。本服务没有工作区或持久化任务队列。相同幂等键用于同一次提交恢复，修改视频或字幕后更换幂等键；真正的去重行为由供应商决定。视频 URL 需在供应商拉取期间有效；接口不返回包含签名源 URL 的编译项目。

翻译/压制请求分别受 `MAX_CONCURRENT_TRANSLATIONS`、`MAX_CONCURRENT_RENDER_REQUESTS` 限制，默认每进程各 1；压制额度约束本服务 HTTP 请求，不限制供应商已提交任务数。鉴权失败返回 401、参数/排版错误返回 422、供应商错误返回 502、未配置能力返回 503、并发不足返回 429。翻译部分失败返回 200 和 `failed_ids`。多个使用者共享同一 API Key 和 MediaKit 账号，任务查询不提供用户级隔离。
