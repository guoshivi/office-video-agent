# 房源视频助手（office-video-agent）

给办公室租赁中介用的一套本地流水线：**拍好的房源视频 → 绑定房源 → 自动口播 → 出竖版成片**。

三个入口共用同一个任务服务，不会出现「网页里是一套、飞书里又是另一套」：

- **WebUI**：<http://127.0.0.1:8770>，在公司用电脑批量处理
- **飞书 / WorkBuddy**：在外面发视频、回房号、收成片
- **命令行**：`ova.bat status`，排查问题或写脚本

## 它到底做了什么

```
中介拍视频
   │
   ├─ WebUI 上传 ─┐
   ├─ 飞书发视频 ─┼→ channel_clips（先存着，等房源）
   └─ ova send ──┘        │
                          │  「A区 中关村大厦 1201」
                          ▼
                    房源精确匹配（809 条，带向量索引）
                          │
                          ├─ 命中唯一 → 生成口播草稿
                          └─ 重号/缺字段 → 返回候选，人来选
                          ▼
                    出片（调用本机「视频工作台」）
                          ▼
                    final.mp4 → 回到对话里
```

口播由房源字段生成，**不会播报租金**（原表口径不统一），
门禁、钥匙、佣金、底价、电话这类信息在整个链路里都被隔离，
既不进向量库，也不出现在成片和 API 返回里。

## 安装

需要 Windows + Python 3.11+。

```bat
start.bat
```

首次运行会建虚拟环境、装依赖，然后起服务到 8770 端口。
浏览器打开 <http://127.0.0.1:8770>，在「系统设置」里填视频工作台地址，再导入房源 Excel。

> 端口固定 8770。8765 被「视频工作台」占用，而且它的健康检查脚本会杀掉所有监听 8765 的进程。

## 三种用法

### 1. 网页

打开 <http://127.0.0.1:8770>：

- **房源库**：809 条房源，支持关键词与语义检索（"A区 100平左右 带家具 朝南"），
  黄色/红色标记的原表行会提示待确认
- **视频任务**：选房源 → 上传视频 → 改口播 → 出片 → 下载成片
- **系统设置**：工作台地址、素材根目录、飞书状态

### 2. 飞书 / WorkBuddy

见 [docs/feishu-setup.md](docs/feishu-setup.md)。两种接法：

- **方案 A（推荐先走）**：用 WorkBuddy 已经接好的飞书通道，不用建应用
- **方案 B**：独立机器人，需要企业自建应用凭据，关掉 WorkBuddy 也能收消息

### 3. 命令行

```bat
ova status                     服务、房源、工作台、待处理视频
ova send  D:\实拍\office.mp4    收到视频（也支持 http 链接）
ova bind  "F区 F14 202-2"       绑定房源，输出口播草稿
ova bind  "102" --pick 2        重号时按候选序号选
ova script V20261101-ABCDE --set "新的口播"
ova render V20261101-ABCDE --wait
ova list --pending             还有哪些视频没绑定
```

全部支持 `--json`；服务在别的机器上加 `--api http://ip:8770`。

## 分镜：一镜一句

工作台是按**句**把文案匹配到镜头的，所以「哪段画面配哪句文案」的把手在文案怎么分句上。

```bat
ova analyze V20261101-ABCDE           :: 按时间切段，每段抽一张代表帧
ova storyboard V20261101-ABCDE --file board.json
```

`board.json` 里每个镜头一句话（≤ 24 字），写完会合成最终 `script` 交给工作台。
句子顺序跟着拍摄顺序走：门头 → 前台 → 工位 → 会议室 → 窗外。

技术选型（为什么用本地多模态向量做归类、用 WorkBuddy 写文案）见
[docs/video-analysis.md](docs/video-analysis.md)。

## 多维表格

任务进度可以写回飞书多维表格。**写入走 REST，不需要事件订阅**，
所以即使用 WorkBuddy 的飞书通道收消息（方案 A），也可以单独建一个
只有 `bitable:app` 权限的应用来写表，两边不会抢长连接。

配置 `.env`：

```
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_BASE_APP_TOKEN=BASCxxx
FEISHU_TASK_TABLE_ID=tblXXX
```

表里需要一列「任务编号」，写入前按它查重、有则更新。
手动同步：`POST /api/tasks/{id}/sync-bitable`。

## 发布到小红书 / 抖音

出片之后可以直接推到平台。两个平台走的路不一样：

- **小红书**：复用开源服务 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)
  （本地 18060 端口），我们只调它的 `publish_with_video`；
- **抖音**：用 CDP 附加到**你日常在用的 Chrome**，操作创作者中心上传页。
  不是新起无头浏览器——抖音对设备指纹更敏感，"借真人"比"伪装真人"活得久。

发布前会先过一遍规则（`app/publish/policy.py`）：标题 20 字上限、
正文 1000 字上限、微信号/手机号直接拦、绝对化用语给警告、每日发帖量和
最小间隔限流。**默认两个平台都是关闭的**，确认账号环境就绪再打开。

完整说明和踩过的坑：见 [docs/publish.md](docs/publish.md)。

```bash
python tools/fetch_publish_tools.py          # 拉取发布服务（二进制不进仓库）
vendor\xhs-mcp\xiaohongshu-mcp.exe -port :18060
```

状态：`GET /api/publish/status`；发布：`POST /api/tasks/{id}/publish`。

## 目录

```
app/
  main.py            API 入口（WebUI、飞书、命令行都调这里）
  channel.py         多入口「先收视频后绑房源」的缓冲层
  shots.py           抽帧分镜：切段、代表帧、一镜一句回填
  services.py        口播生成、脱敏、任务序列化
  excel_import.py    房源 Excel 导入（保留原始行与颜色标记）
  semantic.py        向量 + 规则混合检索
  workbench_client.py 视频工作台适配（建项目/索引/出片/回传）
  publish/           发布到小红书（MCP）/ 抖音（CDP）：policy / mcp_client / xhs / douyin
  feishu/            独立机器人（方案 B）：client / intents / handlers / bitable / bot
  static/            无构建链的原生前端
tools/
  ova.py             命令行入口（send/bind/analyze/storyboard/render）
  vision_probe.py    验证多模态向量模型确实能编码画面
  feishu_check.py    飞书凭据自检，逐步定位缺哪个权限
  fetch_publish_tools.py 拉取发布用的第三方二进制
data/                数据库、上传视频、成片、导出包（不进 Git）
docs/feishu-setup.md 飞书两种接法的完整步骤
docs/publish.md      发布链路、平台差异、已验证项与踩过的坑
```

## 迁移到另一台电脑

1. 拷整个目录（**不含 `.venv`**），或者从 GitHub clone
2. 把 `.env.example` 复制成 `.env`，填工作台地址等本机相关配置
3. 想保留房源和历史任务，就把旧的 `data/` 一起拷过去
4. 跑一次 `start.bat`，在新机器上重新建向量索引（`系统设置 → 重建索引`）

敏感信息全部走 `.env`，`data/` 整个目录不进 Git。

## 已知限制

- 出片依赖本机「视频工作台」，它只接受**授权 NAS 根目录内**的素材；
  本程序会自动读 `/api/roots` 并把素材镜像进去，所以不要手改工作台的目录授权
- 目标时长受原片总长限制，30 秒目标配 20 秒素材只能出 20 秒
- 方案 A 要求 WorkBuddy 一直开着；要无人值守就上方案 B
