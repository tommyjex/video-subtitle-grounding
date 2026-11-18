# Video subtitles — 提取、翻译与压制

基于火山方舟 Ark 和 MediaKit 的独立视频字幕后端。扫描整帧文字，提取时间与位置，按视觉行翻译，再将译文压制到无字幕视频。当前版本 **0.3.8**，仓库与 Python 包名保持 `video-subtitle-grounding`。它不依赖 AD-Creativity 的数据库、资产库或前端。

0.3.8 支持 `render_mode=png|text`，默认 PNG；CRF 为严格整数 0–30，默认 16。两路及 PNG 失败后的 Text 回退统一提交 H.264 与所选 CRF，省略 `video_bit_rate`、`canvas.background_color` 及原生 Text 的 `background_color`，使用接口默认值。PNG 内部自动/人工底板保持，使用真实字体 4 倍采样，自动脚注为 24px；Text 不生成 PNG 图层，保留非 headline 加粗，产品混排整行字体回退返回 `text_product_font_fallback`。HTTP render 顶层 `crf` 是权威输入；SDK 编译/提交通过 runtime video 的 `crf` 传入。排版版本为 `single-line-png-text-default-background-v8`，编码策略为 `configurable-crf-h264-v3`。接口默认底色、真实成片质量与文件大小仍需单独验收。

0.3.3 统一排除独立品牌 Logo 与固定标语，不区分视频类型或出现位置。提示词排除 `Tineco` Logo 和 `Live Easy. Enjoy Life.`；共享 `brand_rules` 模块仅匹配完整品牌文字，分行标语需要原始视觉块、连续行号及共同重叠时间。产品名称、含品牌名的说明和不完整短语保留，原始模型响应不修改。翻译及名称识别默认复用该规则，品牌内容不进入请求或上下文；只有品牌内容时提取正常返回空列表。规则版本为 `tineco-brand-20261117-v1`，排版版本、底板、字体和几何算法不变。`legacy` 模块仅供平台恢复已经冻结的旧任务，不是视频类型开关。离线测试不代表真实模型或成片质量验收。

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
- 自动字体逐条按 `visual_role` 选择：`headline` 使用 Century Gothic Bold，`body`、`footnote`、空值、缺失或未知角色使用 Century Gothic Regular；人工 `style.font` 覆盖优先。
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

0.2.1 支持 `await service.extract(prepared, on_file_uploaded=save_file_id)`。
`save_file_id` 是接受一个 `file_id: str` 的异步函数，上传成功后、轮询和模型请求前调用并等待完成，业务端可在闭包中关联自己的任务 ID 并持久化。
回调失败或取消会停止识别，仍执行 Files 清理。后续识别失败不会撤销已保存的关联；供应商文件在处理后仍会 best-effort 删除，保存的 ID 用于追踪，不代表文件仍可下载。独立 HTTP 提取接口保持原有响应契约，不承担业务任务持久化。

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
docker build -t video-subtitle-grounding:0.3.8 .
docker run --rm --env-file .env -p 127.0.0.1:8010:8010 \
  video-subtitle-grounding:0.3.8
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

修改行为或接口时提升 `pyproject.toml` 和 `__init__.py` 版本；API 版本引用包版本。消费者安装固定版本。0.3.8 对应平台依赖 `video_subtitle_grounding-0.3.8-py3-none-any.whl`，发布步骤见 `docs/platform-integration.md`。先构建 sdist，再从 sdist 构建 wheel；源码导出与 sdist 保持字节一致。

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
    "visual_role": "headline",
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

### 0.2.2 角色字体规则

该版排版版本为 `single-line-adaptive-panel35-role-font-v2`；0.3.0 使用 `single-line-adaptive-panel35-product-font-v3` 并保留以下普通文字规则。`style.font` 未设置、为 `null` 或为空时，逐条按当前 `visual_role` 选择实际字体文件，不按 `block_id`、`line_index`、大小写或文本内容推断：

| 角色 | 字体资源 ID | 实际字体文件 |
| --- | --- | --- |
| `headline` | `CenturyGothicBold` | `https://xujianhua-utils.tos-cn-beijing.volces.com/CenturyGothic/centurygothic_bold.ttf` |
| `body`、`footnote`、空值、缺失或未知角色 | `CenturyGothic` | `https://xujianhua-utils.tos-cn-beijing.volces.com/ECOVACS/centurygothic.ttf` |

Regular 文件的真实字重为 Regular，不是 Light/Thin。本地测量和 MediaKit `font_type` 使用同一实际文件，文件字体的文字元素保持 `bold: false`，不再合成加粗。共享文字块中的标题与正文可使用不同字体。

人工 `style.font` 的已有字体资源 ID 或合法字体 URL 保持优先，不批量改变人工选择；清空后恢复自动规则。历史 `font_type` 是派生摘要，不是人工覆盖；重新计算时按新规则选字体并重新测量，不复用旧 Bold 度量。原始 bbox、时间、角色和译文不变。

自动字体缺字时保留现有 `ALi_PuHui` 回退与警告：标题请求 `bold: true`，其余角色请求 `bold: false`。回退字体本地度量及最终合成粗体效果未验证。Regular 文件可读取和离线测试通过均不代表真实 MediaKit 成片字重验收通过。

客户端保存 `task.task_id`、布局和警告，定期调用查询接口；SDK 也可 `await renderer.poll(task_id=...)`。本服务没有工作区或持久化任务队列。相同幂等键用于同一次提交恢复，修改视频或字幕后更换幂等键；真正的去重行为由供应商决定。视频 URL 需在供应商拉取期间有效；接口不返回包含签名源 URL 的编译项目。

翻译/压制请求分别受 `MAX_CONCURRENT_TRANSLATIONS`、`MAX_CONCURRENT_RENDER_REQUESTS` 限制，默认每进程各 1；压制额度约束本服务 HTTP 请求，不限制供应商已提交任务数。鉴权失败返回 401、参数/排版错误返回 422、供应商错误返回 502、未配置能力返回 503、并发不足返回 429。翻译部分失败返回 200 和 `failed_ids`。多个使用者共享同一 API Key 和 MediaKit 账号，任务查询不提供用户级隔离。

### 0.3.0 产品名称与混合字体

翻译前使用完整 72 条添可命名范式识别名称。字典全名优先，未收录型号、规格和组合由模型判断；结构一致的结果直接保留原名并应用字体，无需新增字典。模型失败时保留字典结果及其他字幕翻译，返回逐条警告。独立规格行需要当前同一 `block_id` 内仍有效的型号，修改或排除型号会取消失效的规格标注。OCR 兼容当前仅包含大小写和重复空格；没有证据的字母替换不自动加入别名。

型号使用 `AvenirLTStdBlack`，规格使用 `AvenirLTStdLight`，普通文字仍按角色和人工设置选择字体。实际文件按共同字号、基线绘制整行透明 PNG 和唯一底板。`product_mentions` 使用 Unicode code point `[start,end)`；`product_text_sha256` 绑定最终译文，`product_names_enabled=false` 可关闭一条字幕的产品字体。`text_runs` 提供真实字体 SHA 和测量；图层缓存键包含字体、内容、区间、视频尺寸、布局和字典版本。

独立 SDK 可调用 `await translator.recognize_products(records, segment_ids=[...])`。HTTP `POST /v1/subtitles/recognize-products` 接受 `{"segments":[{"segment_id":"one","text":"FLOOR ONE i5 Lite","block_id":"product","line_index":0}],"segment_ids":["one"]}`，返回产品标注、指纹、警告与 usage；使用相同 `X-API-Key` 和翻译并发限制，不重新翻译正文。

`compile_subtitle_project()` 新增运行时 `layers`（PNG 字节），图片 `source` 暂为 `subtitle-layer:<hash>`。独立压制调用者需向 `SubtitleRenderService(layer_materializer=...)` 注入异步物化函数，将图层写入自己的私有存储并返回有效 HTTPS URL。HTTP 服务默认没有持久化存储；可在 `create_app(renderer=...)` 注入配置好的 renderer。物化未配置或失败时退回普通文字压制，返回 `product_layer_materialization_unavailable`，不会向供应商提交内部标记，也不会声称混合字体成功。平台已注入 TOS 物化和同源鉴权预览，工作区不保存签名 URL。

0.3.1 保留排版版本 `single-line-adaptive-panel35-product-font-v3` 和字典版本 `tineco-names-20261116-v1`。翻译前自动判定型号/规格，异常自动重试一次；平台在保存译文、POST 排版计算及压制前保存时自动判定历史或修改后的名称，并按文本、同块上下文与字典版本复用结果。GET 读取与纯排版函数不调用模型。产品结果只读展示，不要求人工拆分或校验。无数据库迁移，不自动修改历史成片；重新压制才产生新成片。真实模型识别和 MediaKit 成片质量需另行验证，离线字体像素及模拟 HTTP 不能代替这些证据。

## 0.3.2 视频类型策略

排版版本更新为 `single-line-adaptive-panel35-video-category-v4`，字典版本保持 `tineco-names-20261116-v1`。编译与 render video 字典接受 `video_category`：`product` 为产品视频，`unboxing` 为开箱视频；旧调用缺失字段默认开箱，不按字幕中的产品名称推断任务类型。

产品视频所有有效字幕底板统一 `#00000000`，包括普通文字、标题、正文、脚注、混合字体 PNG 和图层生成/存储/物化失败后的文字回退。人工底板被忽略但不删除，也不改变自动文字色；文字色、字体、字号和布局覆盖保持。近白背景推荐黑字，其余背景推荐白字，不新增描边或阴影。开箱视频保留自动底板和人工覆盖规则。

图层缓存包含类型、有效底板策略与排版版本。平台预览传递当前类型，压制传递冻结任务快照类型；切换类型使当前布局和成片引用失效，但保留输入、译文、逐条样式、识别结果和旧历史图层。旧任务继续按原快照恢复轮询，无数据库迁移或历史成片重写。

平台发布需同步后端契约、前端和固定 wheel，线上只拉平台仓库。回滚需同步恢复适配层、前端及对应旧 wheel；保留已有发布包。离线验收不发起真实模型或 MediaKit 调用，不代表真实供应商成片已验收。
