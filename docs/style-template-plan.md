# 风格模板：参考视频拆解 → 套用到新素材

> 目标：上传一条参考视频（或贴抖音链接），AI 拆出它的**脚本结构、文案口吻、分镜节奏**，
> 然后把这套模板套到另一条房源实拍素材上，直接出片。

---

## 一、先说结论：没有能直接拿来用的轮子

调研了 GitHub 上相关项目，结论是**都不能直接插进本项目**，原因各不相同：

| 项目 | Star | 它做什么 | 为什么不能直接用 |
| --- | --- | --- | --- |
| [hypit-ai/hypit](https://github.com/hypit-ai/hypit) | 23k | 把参考视频克隆成可复用的"视频工作流"（字幕/B-roll/特效） | TypeScript + Remotion + 64 个 headless Chromium 自己渲染，是 WorkBuddy/Claude Code 的 **skill** 形态。它自带渲染器，跟我们已有的视频工作台不是一条链路；只能借鉴拆解思路 |
| [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) | 90k | 主题 → 脚本 → 素材 → 配音 → 成片 | 从主题生成，**不是从参考视频模仿**；自带 TTS + moviepy 剪辑，和工作台职责重复 |
| [modelscope/FunClip](https://github.com/modelscope/FunClip) | 6k | FunASR 转写 + LLM 选段，长视频切片 | 面向"从长视频里挑片段"，不做风格迁移 |
| [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | 5k | 镜头切分 | 房源实拍镜头差异小，项目已明确选了固定时长切段，用不上 |

**所以正确做法**：在本项目里新增"模板"能力，复用已有的 ffmpeg（切段抽帧）和工作台（渲染），
只把"看懂参考视频 + 写文案"这两步交给云端模型。改动集中在一个新模块 + 两个接口，不动现有出片链路。

---

## 二、服务商选型

需要三种能力：**看视频（多模态）**、**听口播（ASR）**、**写文案（文本）**。
理想情况是一家全包，一个 key。

| | 阿里云百炼 | 火山引擎方舟 | Google Gemini |
| --- | --- | --- | --- |
| 视频理解 | Qwen3-VL Plus/Flash、Qwen3.8-Omni | Doubao Seed 2.0 Pro | Gemini 3.1 Pro |
| 视频+音频一次进 | ✅ Omni 系列原生支持 | ✅ | ✅ |
| 价格（视频理解） | Flash 输入 ¥0.15–0.6/百万 token；影视理解另计 ¥2.8/小时 | ≈ $0.71/视频小时（约 ¥5） | $4.32/视频小时（约 ¥31），约为豆包的 6 倍 |
| ASR | Paraformer **¥0.288/小时** | 录音文件识别 ¥0.8/小时 | 自带（含在视频价里） |
| 接口 | OpenAI 兼容 + 原生 SDK | OpenAI 兼容 | 需外网 |
| 国内直连 | ✅ | ✅ | ❌ 需代理 |

**推荐：阿里云百炼。** 理由：视频理解最便宜，ASR 单价是火山的 1/3，
一个 key 同时覆盖三种能力，OpenAI 兼容接口接入成本最低。
（若你已有火山/豆包账号，方案同样成立，只是要开三个模型。）

**单条参考视频成本估算**（30 秒视频）：视频理解 < ¥0.05、转写 ¥0.0024、写文案 ¥0.001，
**合计不到 1 毛钱**。套用模板生成文案每条再约 ¥0.001。

### 链接下载

- 抖音：`yt-dlp` 免费，多数公开视频可直接拿；失效时接第三方解析接口（按次 ¥0.01–0.05）。
- 小红书：反爬重，yt-dlp 基本要到 cookie，**建议手动下载后上传**，不硬做。

---

## 三、技术方案

### 数据模型

```
templates(
  id, name, source_type(upload|douyin), source_url, source_path,
  duration_s, shot_count, spec_json,       -- 拆解结果
  status(pending|analyzing|ready|failed), error, created_at
)
```
`spec_json` 是模板本体：

```json
{
  "structure": [{"role":"hook","share":0.15},{"role":"痛点","share":0.2},{"role":"亮点","share":0.4},{"role":"cta","share":0.25}],
  "shots": [{"index":1,"role":"hook","seconds":4,"sample":"这套办公室，月租只要4000","words":9}],
  "tone": "口语、短句、第二人称、带一个数字钩子",
  "constraints": {"max_words_per_shot": 20, "total_seconds": 30},
  "source_script": "……转写全文……"
}
```

### 流水线

**A. 入库** `POST /api/templates`（multipart 文件 或 JSON 链接）
→ 存 `data/templates/<id>/source.mp4`，状态 pending。

**B. 拆解**（后台线程）
1. `ffprobe` 拿时长 → 按 2 秒抽帧（最多 15 帧，控 token）
2. ASR 拿逐句时间戳（Paraformer 录音文件识别）
3. 多模态模型看「抽帧 + 转写稿」→ 输出上面的 `spec_json`
   - 提示词明确要求：只描述可复用的结构，不复述具体房源事实；不编造
4. 落库，状态 ready

**C. 套用** `POST /api/tasks/{task_id}/apply-template {template_id}`
1. 取任务绑定房源的公开字段（复用 `public_property`，敏感字段天然被过滤）
2. 文本模型按 `structure` 逐镜生成文案：一镜一句、每句 ≤ `max_words_per_shot`、禁编事实
3. 校验：句数 = 镜头数、字数超限自动重写一次
4. 写 `storyboard`（现有接口）→ 现有 `render` 出片

### 分工边界（重要）

- **能模仿**：脚本结构、句子长短、口吻、每镜时长、CTA 位置。
- **不能模仿**：运镜、调色、B-roll。工作台的渲染参数（字幕字号/位置/停顿）可部分映射，
  但调色和转场它不开放——这点必须跟使用者讲清楚，不然会期待落空。

---

## 四、接入需要

- `.env` 新增：`DASHSCOPE_API_KEY=`（必填）、`STYLE_LLM_MODEL=qwen3-vl-plus`、`STYLE_TEXT_MODEL=qwen-plus`、`STYLE_ASR_MODEL=paraformer-v2`
- 依赖：`openai`（只用它的兼容客户端）、`yt-dlp`（链接下载，可选）
- 不需要 GPU、不需要本地模型（走 API），现有 93 个测试不受影响

## 五、风险

1. **版权**：模仿结构合规，直接搬运画面/文案不合规。模板只存结构和风格描述，不存原片画面。
2. **链接下载**：抖音接口会变，失败要有明确提示，不能静默出错。
3. **事实准确性**：文案只能来自房源字段或画面真实可见的内容，沿用项目现有的 hygiene 校验。
