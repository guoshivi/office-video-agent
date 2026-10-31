# 发布到小红书 / 抖音

出片之后把成片推到平台，是整条链路的最后一段，也是**唯一不可逆的一段**：
发出去的内容会带着房源信息对外露脸，错了就是真实的经营风险。
所以这里的设计原则是「能自动化的自动化，该拦的死拦」。

## 一、两个平台的实现方式（不一样，别统一）

| | 小红书 | 抖音 |
|---|---|---|
| 官方发布 API | 无公开接口 | 有 `video.create.bind`，但要企业资质 + 应用审核 |
| 我们的做法 | 复用 `xpzouying/xiaohongshu-mcp` | CDP 附加到真实 Chrome |
| 实现文件 | `app/publish/xhs.py` | `app/publish/douyin.py` |
| 端口 | 18060（MCP Streamable HTTP） | 9222（Chrome DevTools） |
| 登录 | 扫码一次，cookie 持久化 | 在专用 Chrome 窗口登一次 |

### 小红书为什么用 MCP

登录态、请求签名、风控对抗那个 exe 里都做完了。我们只实现了一个两百来行的
MCP 客户端（`mcp_client.py`）去调它，比自己写浏览器自动化省掉最脏的一块。

### 抖音为什么用 CDP 而不是 MCP

抖音那个 MCP（`279458179/douyin-mcp`）底层是 go-rod **新起一个干净浏览器 + stealth 补丁**，
本质是"伪装成真人"。CDP 附加到朋友日常在用的 Chrome 是"本来就是真人"——
指纹、cookie、历史记录、字体渲染全是真的。抖音的设备指纹和行为检测比小红书硬，
这个差别直接决定账号活不活得下来。

附带好处：窗口就在屏幕上，出问题一眼看见；弹验证码时人也在现场，顺手点掉。

**Chrome 的坑**：111 以后如果已有实例在跑，`--remote-debugging-port` 会被直接忽略。
必须用独立 `user-data-dir` 起一个新实例：

```
chrome.exe --user-data-dir="D:\luo\office-video-agent\data\chrome-profile" --remote-debugging-port=9222
```

## 二、已验证 / 未验证

已验证（2026-11-01，v2.5.5 Windows x64）：

- [x] exe 可直接运行，首次自动下载内置浏览器（约 140MB，仅一次）
- [x] MCP `initialize` 握手成功，`serverInfo: xiaohongshu-mcp 2.0.0`
- [x] `tools/list` 返回 19 个工具，含 `publish_with_video`
- [x] `check_login_status` 正确返回「❌ 未登录」
- [x] `get_login_qrcode` 返回二维码 image 内容块（base64 PNG，约 5.8KB）
- [x] 客户端能解析 SSE 与 JSON 两种响应
- [x] 抖音 CDP 探测在未启动时给出准确提示

未验证（需要真实账号）：

- [ ] 登录后的 `publish_with_video` 实际返回文本格式（决定能否拿到笔记链接）
- [ ] 抖音上传页的 DOM 选择器（`douyin.py` 里集中放在常量里，首次使用要现场校准）
- [ ] 抖音发布时的短信验证是否阻塞无人值守

## 三、踩过的坑

1. **本机全局代理会劫持 localhost**。这台机器 `HTTP_PROXY=127.0.0.1:53011`，
   `curl localhost:18060` 直接 502。所有本地服务调用都必须 `trust_env=False`，
   否则朋友电脑上装了 Clash/加速器就是同样症状。代码里 `httpx.Client(trust_env=False)`。

2. **MCP 的二维码在 `image` 内容块里，不在 `text` 里**。只读 text 会拿到
   「请用小红书 App 扫码登录 👇」而没有任何图片。`ToolResult` 现在同时返回 text 和 images。

3. **小红书标题硬顶 20 字、正文 1000 字**。超长不报错而是被截断得莫名其妙。
   `policy.py` 负责裁到合规长度，且优先在句末切，不把一句话砍半。

4. **中文路径不稳定**。官方排障第一条就是"确认路径中没有中文字符"。
   所以发布前统一 `stage_video()` 复制到 `data/publish_stage/<任务号>.mp4`
   （纯 ASCII、无空格），顺带避免上传期间原文件被改写。

5. **同一账号不能多端网页同时登录**。登了 MCP 之后就不要再开浏览器版小红书，
   否则 MCP 会被踢下线。这条要提前告诉使用者，不然会反复掉登录。

6. **`Path("")` 等于当前目录且 `exists()` 为真**。判"有没有成片"必须判空字符串
   并检查 `is_file()`，否则空路径会被当成有视频。

## 四、发布前的规则校验

`policy.py` 分两级，blocking 不改就不让发，warn 只是提示：

| 级别 | 触发条件 |
|---|---|
| blocking | 出现微信号 / QQ / 手机号 / "加我""加V" |
| blocking | 成片不存在、文件过小、标题或正文为空 |
| warn | "私信我""私聊我"（强引导，可能限流） |
| warn | 绝对化用语：最低价、第一、唯一、保证、包过、稳赚…（广告法 + 平台限流） |
| warn | 标题/正文超长（已自动裁到合规） |

"私信咨询"只 warn 不拦——默认的 `public_contact` 就是"欢迎私信咨询房源详情和看房安排"，
拦掉会把自己的默认文案挡死。真正会被平台打的是把**微信/手机号**写进正文。

## 五、频率控制

中介号不是矩阵号。`publish_daily_limit`（默认 3 条/天）和
`publish_min_interval_minutes`（默认 60 分钟）都在设置里可改。

不要照着开源项目 README 里"每天 50 篇"的玩法来，那是成熟号的量，
中介新号这么发必进风控。

## 六、操作步骤

```bash
# 1. 拉取发布服务（仓库里不含二进制）
python tools/fetch_publish_tools.py

# 2. 启动小红书发布服务
vendor\xhs-mcp\xiaohongshu-mcp.exe -port :18060

# 3. 启动带调试端口的 Chrome（抖音用）
chrome.exe --user-data-dir="D:\luo\office-video-agent\data\chrome-profile" --remote-debugging-port=9222

# 4. 看状态
curl -s --noproxy '*' http://127.0.0.1:8770/api/publish/status

# 5. 扫码登录（二维码约 60 秒过期，扫之前现取）
curl -s --noproxy '*' -X POST "http://127.0.0.1:8770/api/publish/login-qrcode?platform=xiaohongshu"
```

发布：

```bash
curl -s --noproxy '*' -X POST http://127.0.0.1:8770/api/tasks/<task_id>/publish \
  -H "Content-Type: application/json" -d '{"platforms": ["xiaohongshu"]}'
```

## 七、和审核台的衔接（待做）

当前发布是**手动触发**的。接入多维表格审核台之后应该是：

出片 → 写多维表格（待审）→ 预检 + 人裁决 → 审核=通过 → 轮询到 →
自动调发布 → 回写链接与互动数据

`publish_auto_after_review` 设置项已经预留，默认 0（关）。
