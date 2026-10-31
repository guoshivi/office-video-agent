# 运维手册

给「要把这套东西跑起来、跑下去」的人看。装机细节和平台配置在各自的文档里，
这里只管：怎么启动、每天做什么、坏了怎么办、以后怎么改。

## 一、模块地图

先给一张「改东西该去哪」的表。这份清单是对齐模块边界的依据——
新增功能时先看它属于哪一层，别把逻辑塞进 `main.py`。

```
app/
├── main.py              装配层：只做 include_router、挂静态文件、启动后台线程
├── db.py                表结构与默认设置（唯一知道 DDL 的地方）
├── services.py          任务与房源的基础读写
├── excel_import.py      房源库导入（标题 + 第 2 行表头）
├── channel.py           多渠道收片（本机上传 / 飞书 / 工作台回传）
├── hygiene.py           措辞卫生：内部提醒句的词表与摘句（出片前 + 审核时共用）
├── shots.py             视频分析、抽帧、分镜
├── semantic.py          本地向量检索
├── workbench_client.py  对接现有的视频工作台（出片）
├── publish/             发出去
│   ├── policy.py        发布前规则校验（字数、引流词、路径暂存）
│   ├── mcp_client.py    两百行的 MCP 客户端（JSON + SSE 双解析）
│   ├── xhs.py           小红书：调本地 xiaohongshu-mcp
│   ├── douyin.py        抖音：CDP 附加真实 Chrome
│   └── __init__.py      组装标题/正文、频率控制、publish_task()
├── review/              发之前先审
│   ├── precheck.py      预检引擎（纯函数）
│   ├── store.py         队列持久化（只管存取）
│   ├── service.py       编排（唯一知道「流程」的地方）
│   └── bitable_bridge.py 多维表格镜像 + 裁决回拉
├── api/                 路由（按领域拆开，不再堆在 main.py）
│   ├── publish.py
│   └── review.py
├── feishu/              飞书：消息入口 + 多维表格写入
└── static/              WebUI（无框架、无构建链、无 CDN）
```

三条不许破的边界：

1. **`store.py` 不许 import `publish`。** 存储层一旦知道「通过后要发」，换发布实现就要动数据库代码。
2. **`precheck.py` 不许碰数据库和网络。** 它是纯函数，才能被单测直接跑一百遍。
3. **`main.py` 不许有业务逻辑。** 加功能 = 新建模块 + 在 `app/api/` 加路由 + 在 `main.py` 注册一行。

## 二、启动

```bash
start.bat                 # 建 venv（第一次）+ 起服务 + 开浏览器
```

手动起：

```bash
./.venv/Scripts/python.exe -m uvicorn app.main:app --port 8770
```

三个可选的外部依赖，都是「不开也能用，开了才多一项能力」：

| 依赖 | 用来做什么 | 打开方式 |
|---|---|---|
| 视频工作台 | 真实配音与出片 | 设置页填 API 地址，或 `.env` 的 `WORKBENCH_URL` |
| `vendor/xhs-mcp/xiaohongshu-mcp.exe` | 小红书发布 | `python tools/fetch_publish_tools.py` 下载后双击；端口 18060 |
| 专用 Chrome 窗口 | 抖音发布 | 见下文 |

抖音那个 Chrome 必须用**独立 profile** 起，否则已有实例会让调试端口失效：

```
chrome.exe --user-data-dir="D:\luo\office-video-agent\data\chrome-profile" --remote-debugging-port=9222
```

在弹出来的窗口里登一次抖音，之后这个窗口要保持开着（可以最小化）。
`python tools/douyin_probe.py` 会探测上传页并截图，用来校准选择器。

## 三、日常流程

```
导入房源 Excel → 建任务（选房源 + 上传实拍） → 出片 → 自动入审核队列（并预检）
   → 人在审核台看一遍 → 通过 → 发布面板发小红书/抖音
```

每天真正会做的只有两件事：

1. **`ova review`**（或打开审核页）看一眼有没有待审的。
2. 通过了就看**发布面板**的两个平台是不是在线、额度还剩几条。

`PUBLISH_AUTO_AFTER_REVIEW=1` 可以让「通过」直接触发发布，但**建议一直保持 0**，
至少跑通一个月再说。原因很简单：机器发出去的东西删不掉。

## 四、坏了怎么办

先看 `data/server.log`，再看这张表。

| 现象 | 多半是什么 | 怎么办 |
|---|---|---|
| 页面能开，所有接口 500 | 数据库被锁或表缺字段 | 删 `data/office_video_agent.db` 前先备份；已存在的库不会自动加列 |
| 小红书「未登录」 | MCP 服务重启了，cookie 丢了 | 发布面板点「扫码登录」，扫完点刷新 |
| 小红书二维码不出来 | MCP 不可达 / 端口不对 | `curl --noproxy '*' http://127.0.0.1:18060/mcp` 看有没有响应 |
| 本地请求 502 | 本机全局代理把 localhost 也代理了 | 代码里已 `trust_env=False`；curl 调试要加 `--noproxy '*'` |
| 抖音「端口不通」 | 没起 Chrome 或用了已有实例 | 按上面的命令用独立 `user-data-dir` 重开 |
| 抖音「找不到标题输入框」 | 上传页改版 | 跑 `python tools/douyin_probe.py`，按截图改 `TITLE_SELECTORS` |
| 发布成功但状态没变 | 提交了但读不出结果 | 看 `publish_records` 里的 `uncertain` 标记；去平台后台确认，别急着重发 |
| 审核队列一直是空 | 任务没出片，或成片路径是空串 | `ova review --set ''` 看全部；确认 `output_video_path` 不是空 |
| 预检全是 warn「读不出视频参数」 | ffprobe 路径不对 | 设置页填 `FFMPEG_PATH`，或让工作台自带的那个上 PATH |
| 多维表格没有新行 | 缺凭据 / 表 ID 错 / 缺列 | 设置页看「审核镜像」那张卡的状态提示 |
| 表格上改了「通过」但本地没变 | 镜像轮询没开 | `REVIEW_BITABLE_SYNC=1` 后重启 |
| 飞书没反应 | 长连接掉了 | 设置页点「重连长连接」 |

### 发布出问题时的判断顺序

发布是唯一不可逆的动作，出问题别急着重试。按这个顺序看：

1. `data/publish/records`（或 `/api/publish/records`）里这条是什么状态。
2. 状态是 `uncertain` → **去平台后台看**。提交大概率成功了，只是没读回结果。重发会变成两条。
3. 状态是失败且提示规则校验 → 那是 `policy.py` 拦的，改文案，不是接口坏了。
4. 状态是失败且提到对应平台 → 看 [publish.md](publish.md) 的踩坑清单。

## 五、备份与迁移

要备份的就是 `data/` 一个目录：

```
data/office_video_agent.db   数据库（房源、任务、审核队列、发布记录都在里面）
data/uploads/                上传的实拍原片
data/workbench_outputs/      工作台成片
data/publish_stage/          发布前置暂存的成片副本
data/chrome-profile/         抖音登录态（换机器要重新登）
```

`data/` 和 `vendor/` 都不进 git。搬家时整个拷过去，`.env` 一起拷，
`.venv` 不要拷（换机器重建）。

## 六、测试

```bash
./.venv/Scripts/python.exe -m pytest -q
```

注意：**必须用 venv 里的 python**，系统 python 没装 pytest。

覆盖面：导入、任务流转、发布规则、MCP 客户端解析、预检、队列状态机、多维表格桥接降级。
真实的平台交互（扫码、点发布按钮）不在单测里，靠联调验。

## 七、以后要加东西的话

几个已经预留好的扩展点：

- **新平台**：在 `app/publish/` 加一个 `xxx.py`，实现 `status()` 和 `publish_video()`，
  然后在 `__init__.py` 的 `platform_status()` / `publish_task()` 里登记。规则校验复用 `policy.py`。
- **新预检项**：在 `precheck.py` 里加一个 `_check_xxx()` 并追加 `Check(...)`，
  别忘了补一条单测——预检的价值全在「它真的会拦」。
- **新的审核入口**（比如企业微信群）：调 `service.approve()` / `service.reject()` 即可，
  不要在入口里直接改 `store`，否则会绕过「blocking 需要显式越过」这条规则。
- **换掉多维表格**：`bitable_bridge.py` 是唯一和表格打交道的地方，
  换 Notion / 语雀只要保住 `push_item` / `pull_decisions` 两个函数的签名。
