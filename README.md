# QLark 云雀

基于 [NapCat](https://github.com/NapNeko/NapCatQQ) 的 QQ 聊天机器人 **Windows 桌面客户端**。
适配 **OpenAI API 格式** 的 Key 调用，主打**多人设对话**与**记忆功能**，支持群聊与私聊。

- 语言：Python 3.13 ｜ 界面：pywebview（Edge WebView2）｜ 协议：OneBot 11
- 当前进度：**M1–M6 全部完成**（进程管理 / 消息闭环 / 配置与人设 / 记忆系统 / 图片理解 / 打包分发）

---

## 目录结构

```
QLark/
├── src/qlark/
│   ├── __main__.py          命令行入口（--headless / --self-check / --debug）
│   ├── app.py               应用装配：存储 + 日志 + NapCat 管理 + 机器人 + 界面桥接
│   ├── api.py               暴露给前端的 JS API
│   ├── bot.py               机器人运行时：把事件通道与消息流水线绑在一起
│   ├── paths.py             程序目录 / 用户数据目录 / NapCat 运行时路径
│   ├── settings.py          配置默认值与类型化读写
│   ├── version.py           版本与常量
│   ├── logging_setup.py     把 logging 接进 LogBus
│   ├── core/models.py       状态机阶段、日志行、环境自检、AppState
│   ├── storage/             SQLite（WAL）+ schema.sql（需求规格 v1.1 §7 的 8 张表）
│   ├── napcat/
│   │   ├── launch.py        启动参数（可注入假 NapCat）
│   │   ├── process.py       静默 spawn / 杀父进程树 / 孤儿清理 / stdout 读线程
│   │   ├── logbus.py        日志环形缓冲 + 落盘轮转
│   │   ├── envcheck.py      首次运行环境自检
│   │   ├── netconfig.py     onebot11_<QQ>.json 合并写入（动作通道 + 事件通道）
│   │   ├── qrcode.py        二维码文件轮询（防残留、防过期）
│   │   ├── patterns.py      NapCat 输出特征串（全部来自实测）
│   │   └── login.py         启动 / 登录状态机
│   ├── events/server.py     接收 NapCat 事件推送的本地 HTTP 服务
│   ├── onebot/
│   │   ├── client.py        OneBot 11 HTTP 客户端（发动作）
│   │   ├── events.py        事件模型与解析
│   │   ├── segments.py      消息段解析 + 降级映射表
│   │   └── sender.py        回复发送（分段 + 群聊引用）
│   ├── llm/client.py        OpenAI 兼容客户端（URL 归一化 + 错误分类 + 退避重试）
│   ├── chat/
│   │   ├── pipeline.py      消息流水线：过滤/去重/触发/串行/回复/落库
│   │   ├── queue.py         每会话串行 + 全局并发上限
│   │   ├── dedup.py         消息去重
│   │   ├── store.py         记忆落库、摘要读写与人设读取
│   │   ├── summarizer.py    合并式摘要（失败不丢原文）
│   │   ├── images.py        图片：下载 → 缩放 → JPEG → data URL
│   │   ├── moderation.py    速率限制 + 敏感词过滤
│   │   ├── personas.py      人设 CRUD、删除级联、白名单
│   │   ├── commands.py      QQ 指令（/person /status /reset /help）
│   │   └── context.py       上下文组装（人设 + 长期记忆 + 最近 W 轮）
│   ├── assets/              程序图标（tools/make_icon.py 生成）
│   └── web/                 前端（纯 HTML/CSS/JS，无框架无外链）
├── tests/                   pytest（见下方「测试」一节的实际数量）
├── tools/
│   ├── fetch_napcat.py      一键准备 NapCat 运行时（下载 + 解压 + 补 DLL + 校验）
│   ├── set_model.py         命令行设置模型配置（M3 的模型配置页之前的临时入口）
│   ├── fake_napcat.py       假 NapCat：无运行时也能端到端验证（事件推送 / 消息注入 / get_image）
│   ├── fake_openai.py       假 OpenAI 服务：无 API Key 也能端到端验证对话
│   ├── ui_check.py          界面布局自检：找出被裁切的内容、验证该滚动的能滚动
│   ├── make_icon.py         生成程序图标（多尺寸 ico）
│   ├── build_exe.py         一键打包成免安装目录（PyInstaller）
│   └── showcase.py          界面自动化：切到指定视图便于截图验证
├── QLark.spec               PyInstaller 打包配置
├── docs/                    验收截图
├── runtime/napcat/          NapCat 运行时（约 300MB，不入库）
└── Readme/                  需求与技术文档
```

---

## 快速开始

### 1. 准备 NapCat 运行时

`runtime/` 不在版本控制中（约 437 MB），用一条命令准备好：

```powershell
.\.venv\Scripts\python.exe tools\fetch_napcat.py
```

脚本完成技术文档的步骤 1~4：下载 `NapCat.Shell.Windows.Node.zip`（约 111MB，
GitHub 镜像优先、失败自动回落）→ 解压到 `runtime/napcat/`（自动处理 zip 内多套一层目录）
→ 自动定位本机已安装的 QQ 并补齐 `crypto.dll` / `ssl.dll`（**官方 Node 包不含这两个文件**）
→ 校验 7 个必需文件。

其他开关：`--check-only` 只校验、`--dll-only` 只补 DLL、`--force` 强制重装、`--version` 指定版本。

<details>
<summary>手动准备步骤（脚本不可用时）</summary>

1. 下载 `NapCat.Shell.Windows.Node.zip`（约 111 MB）解压到 `runtime/napcat/`，
   确保 `node.exe` 就在该目录**直下**
2. 安装 VC++ 运行库（`vc_redist.x64.exe`）
3. 从 QQ 安装目录 `<QQ安装目录>\versions\<版本>\resources\app\` 补齐
   `crypto.dll` / `ssl.dll` 到 `runtime/napcat/`

</details>


### 2. 安装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

### 3. 配置模型（OpenAI 兼容）

在界面里点「模型配置」页填好 Base URL / API Key / 模型名，然后点**「测试连接」**
确认能通、点**「应用」**保存（**不需要重启**）。

> 界面还没起不来时，也可以用命令行：
> ```powershell
> .\.venv\Scripts\python.exe tools\set_model.py `
>     --base-url https://api.deepseek.com --api-key sk-你的key --model deepseek-chat
> .\.venv\Scripts\python.exe tools\set_model.py --test
> ```

### 4. 运行

```powershell
python run.py                 # 启动图形界面（正常使用）
python run.py --self-check    # 只跑环境自检
python run.py --headless      # 不启界面，在控制台跑完整启动/登录流程
python run.py --debug         # 打开 WebView2 开发者工具
```

首次运行会弹出**模态登录面板**：优先快速登录（已保存账号时免扫码），失败自动降级扫码。

### 机器人怎么工作

| 规则 | 行为 |
|---|---|
| 群聊触发 | **必须 @ 机器人**（@全体成员不算；纯文本 "@昵称" 不算） |
| 私聊触发 | 无限制 |
| 群聊回复 | **只回复，不引用原消息**（群里引用看着很吵；想引用可在「模型配置 → 对话行为」打开） |
| 群聊身份 | **消息带发送者名字**，机器人看到的是 `小明：你好呀`（见下） |
| 私聊回复 | 不带名字、不引用 |
| 超长回复 | 自动按段落 / 句末标点分段发送 |
| 不支持的消息 | 语音 → `[语音消息]`、视频 → `[视频]`、表情 → `[表情]`、文件 → `[文件：名字]`… |
| 记忆 | 每个群一份、每个人设一份、不同群互不干扰；私聊按用户隔离；默认携带最近 20 轮 |
| 调用失败 | 可重试的错误自动退避重试；私聊会告知原因，**群聊不刷屏** |
| 一键暂停 | 仪表盘「运行信息」里的「暂停回复」按钮 |

#### 为什么群聊要带发送者名字

一个群**共用一份记忆**（规格 §6.1），所有人的话都落在同一条上下文里。如果不带名字，
机器人看到的是这样一团：

```
user: 你好呀
assistant: 你好！
user: 我不吃香菜
user: 我想吃火锅
```

它根本不知道这几句分别是谁说的——会把 A 的忌口记到 B 头上，也没法回答"刚才谁说要吃火锅"。
加上名字之后，历史本身就是清楚的：

```
user: 小明：你好呀
user: 小红：我不吃香菜
user: 小明：我想吃火锅
```

**私聊不加**：那里只有一个人，加了纯属浪费 token。这个开关在
「模型配置 → 对话行为 → 群聊带上发送者名字」，**建议保持开启**。

### QQ 里的指令

| 指令 | 说明 | 权限 |
|---|---|---|
| `/person N` | 切换**本会话**的人设（不带参数则列出全部） | 群聊默认**仅白名单 QQ**；私聊始终允许本人 |
| `/status` | 查看当前会话身份 / 人设 / 记忆条数 | 所有人 |
| `/reset` | 清空本会话（当前人设）的记忆 | 与 `/person` 相同 |
| `/help` | 指令列表 | 所有人 |

- 群聊里要 @ 机器人（`@机器人 /person 2`）；私聊直接发。
- 全角 `／` 也认。
- **指令不会写进记忆**，也不会消耗模型 Token。

### 人设

- 出厂自带 **人设1**（不可删除，新会话默认用它）；可增删，**上限 99 个**。
- 每个人设可配：名称 / 系统提示词 / 启用 / 模型覆盖 / 温度覆盖 / 最大 Token 覆盖。
- **人设作用域是会话独立的**：每个群、每个私聊用户各自记住自己当前用的人设。
- 删除人设时：正在使用它的会话**静默切回人设1**，该人设的**全部记忆一并删除**，界面编号自动重排。
- 空人设 = 不注入 system 提示词（**不是**发一条空的 system，部分 OpenAI 兼容服务会因此报错）。


### 记忆（M4）

机器人**有长期记忆**，而且是"会自己整理"的那种：

- 每次成功问答都会落库（模型调用失败的那一轮**不写**，避免留下"有问无答"的空洞）。
- 未压缩原文攒满 **W 轮**（默认 20，可改）时，自动把它们连同**上一版摘要**一起
  交给总结模型，压成一段不超过 300 字的摘要。
  这就是**合并式**压缩：`新总结 = 总结(老总结 + 新 W 轮)` —— 老总结被替换，
  但它的信息已经并进新总结，所以记忆视野**无限延伸**（代价是越久远越模糊）。
- 组装请求时：`[system] 人设` + `[system] [长期记忆] <摘要>` + 最近 W 轮原文 + 本轮消息。
  摘要**不占轮次**；超出上下文预算时**从最早的原文开始丢，摘要始终保留**。
- **压缩失败不丢原文**：模型调用失败（超时、限流、5xx）只标记"待重试"，
  一条原文都不删，下一轮自动再来。宁可上下文暂时变长，也不能因为一次网络抖动
  就把你的记忆弄丢。

「记忆面板」页可以按会话查看 / 编辑 / 删除原文与摘要，也可以手动触发压缩。

> 「自动总结」开关管的是**要不要自动压**。关掉之后手动点「立即压缩」照样能压。

### 图片理解（M5）

群里发图，机器人能看懂：

1. **收到就立刻下载**。QQ 的图片链接约 **2 小时**后失效，等你过一会儿再问
   "刚才那张图是什么"，链接早就没用了。
2. 等比缩放到长边 ≤ 1024，统一转 JPEG（透明度贴白底，动图取首帧），
   再转成 base64 data URL 交给模型。
3. **记忆里只存 `[图片]`** —— base64 动辄几百 KB，写进数据库会让库迅速膨胀，
   而且历史图片对后续对话几乎没有价值。

降级路径都是通的：图片下不动 / 没有视觉模型 / 模型不支持图片 → 按 `[图片]`
占位继续对话，**消息不会失联**。模型明确表示不支持图片时，会自动降级成纯文本
重试一次，并在日志里告诉你「去配一个视觉模型」。

### 风控（M6，建议别关）

| 机制 | 默认 | 说明 |
|---|---|---|
| **速率限制** | 每会话 **12 条/分钟** | QQ 对短时间高频回复非常敏感。**被限流甚至被封，往往不是因为说了什么，而是因为回得太多。** 一个人自言自语刷屏时，机器人跟着刷是最危险的 |
| **敏感词过滤** | 关闭 | 命中就**直接忽略**（不回复、不调模型、不入记忆）。默认关闭是因为强行替用户决定哪些词不能说很容易误伤 |
| **风控提示** | 首启强制确认 | 明确告知第三方 QQ 机器人**可能触发风控导致账号被限制或封禁** |

限流是**按会话**算的：一个群刷屏不该把另一个群的正常对话一起饿死。

### 首次登录可能出现的"两次扫码"（正常现象）

首次扫码登录成功后，QLark 需要写入 OneBot 网络配置并重启 NapCat——**协议端默认不开
任何端口，而配置文件不支持热重载**（技术文档坑 7、§3.3）。

问题在于：刚登录就重启时，QQ 可能要求 `登录需要手Q验证`，导致快速登录失败、NapCat
回落到二维码，于是你会看到**二维码出现第二次**。

- QLark 已做缓解：写配置前会等待数秒让会话与凭据落盘，并优先复用已有的网络配置
  （已配置就不再重启）。
- 如果 QQ 仍然要求验证，那属于账号侧的风控行为，客户端无法绕过 —— **再扫一次即可**，
  这次端口已经配好，登录后就直接就绪。

登录成功一次后账号会被持久化，**之后每次启动都是免扫码快速登录**。

### 扫码登录后提示"无法确认登录的是哪个 QQ"

QLark 判断"登录的是哪个号"有三条独立路径，正常情况任意一条成功即可：

1. **NapCat 输出**：登录后它会打印 `[Core] [Config] 配置文件…napcat_<QQ>.json加载`，这行里带着 QQ 号；
2. **文件基线**：`napcat_<QQ>.json` / `napcat_protocol_<QQ>.json` 出现新写入；
3. **OneBot 探针**：直接问端口上的 `get_login_info`。

三条全失败才会报这个错。已知会导致失败的原因：

- **系统代理劫持了本机请求**。Windows 的"代理绕过列表"里通常写了 `127.*`，但
  **Python 读注册表时不解析这个白名单**——于是连 `http://127.0.0.1:3000` 都被丢给代理，
  探针超时。QLark 已对本机 OneBot 调用强制 `trust_env = False`（模型请求仍走代理）。
  如果你在别处也遇到"本机接口偶发超时"，可以用这条命令自查：
  ```powershell
  python -c "import requests; print(requests.utils.getproxies())"
  ```
- **NapCat 版本输出格式变了**（升级过 NapCat）。这时三条路径的日志解析都失效，
  请把「日志」页的 NapCat 输出发给开发者。

> 这个提示本身不会中断程序：点「重新启动」再扫一次即可，日志页有完整的 NapCat 输出。

### 卡在"已登录，正在初始化…" / 消息发不出去

说明动作端口（默认 3000）没有被 NapCat 监听。**先看「日志」页有没有这一行**：

```
OneBot 服务端已就绪：http://127.0.0.1:3000
```

没有它，就说明 QLark 连不上 NapCat。已知原因：

- **NapCat 配置里两条通道同名**。这是本项目 M2 阶段踩过的真实坑：引擎按 `name`
  把网络适配器存进 Map，`httpServers` 和 `httpClients` 同名时后者会**覆盖**前者，
  动作端口就再也不会监听 —— 而 NapCat 依然会打印"HTTP服务…已启动"，极具误导性。
  现行版本已给事件通道取名 `qlark-events`，并在启动时自动清掉同名的旧条目。
  如果你的 `config/onebot11_<QQ>.json` 里两条通道都叫 `qlark`，重启一次 QLark 即可自动修复。
- **端口被别的程序占用**。QLark 会在写配置前自动换一个空闲端口；若仍失败，
  可在「模型配置 → NapCat 连接」里手动改。

自查端口是否真的在监听：

```powershell
netstat -ano | findstr ":3000"
```

> 注意：**NapCat 打印的 `HTTP服务: 127.0.0.1:3000, : 已启动` 不能作为判据** ——
> 那只是它遍历配置拼出来的字符串，配置里写了 `enable: true` 就打印，跟实际监听无关。

### 机器人"在线但完全不回复"

按顺序看这两件事：

1. **仪表盘的「事件服务」是不是「监听中」**。不是的话说明 3001 没起来。
2. **NapCat 日志里有没有 `Parse Error: Expected HTTP/`**。有的话是事件推送链路坏了
   （历史上是鉴权方式不匹配 + 响应格式问题，已修）。

自查事件服务到底收没收到东西：

```powershell
curl http://127.0.0.1:3001/_qlark/health
```

正常应返回 `{"ok":true,...,"received":N,"rejected":0}`。
**`rejected` 不为 0 就说明鉴权在拒事件**——那机器人永远收不到消息。

---

## 开发

### 测试

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m pytest -q
```

测试**不需要真实 NapCat**：`tools/fake_napcat.py` 复刻了 NapCat 的可观测行为
（快速登录 / 二维码回落 / 账号落盘 / OneBot HTTP 接口），状态机能被完整验证。

### 准备 NapCat 运行时（一键脚本）

见 [快速开始 §1](#1-准备-napcat-运行时)。

### 界面预览

直接双击 `src/qlark/web/index.html` 即可用浏览器预览——检测不到 `pywebview` 时会自动
进入 mock 模式（右上角显示「预览模式」徽章）。

### 界面布局自检

```powershell
python tools\ui_check.py --view login      # dashboard | logs | model | persona | memory | login
python tools\ui_check.py --view model --js-file probe.js   # 额外跑一段 JS 并把结果打出来
```

用**真实后端**打开界面，检查有没有内容被 `overflow:hidden` 裁掉、该滚动的容器能不能滚。
退出码 1 表示发现裁切。这类"内容被压扁/裁掉"的问题人眼很难在每个窗口尺寸下发现。

`--js-file` 用来验证**数据是不是真的从后端绑上来了**（例如表单里显示的是真实配置
还是内置预览数据）——`tests/test_m3_frontend.py` 就靠它锁死了一个真实事故。
> 用 `--js-file` 而不是 `--js`：PowerShell 会把命令行里的双引号吃掉，直接把 JS 当参数传会被搞坏。

### 界面截图（真实后端）

```powershell
$env:QLARK_SHOWCASE='logs'   # logs | login | model | persona | memory
python tools/showcase.py
```

### 打包成 exe

```powershell
python tools\build_exe.py --check        # 只做前置检查
python tools\build_exe.py                # 打包（**默认带上 NapCat 运行时**）
python tools\build_exe.py --no-runtime   # 只打程序本体，运行时另配
```

产物在 `dist/QLark/`，双击 `QLark.exe` 即可运行（**没有黑框**）。
带运行时约 365 MB，不带约 39 MB。

几个刻意的取舍：

- **默认就带 NapCat 运行时**。最初这里是"默认不带、要加 `--with-runtime`"，
  结果第一次交付出去的包双击就报「环境自检未通过：NapCat 运行时目录不存在」——
  一个跑不起来的"免安装包"等于没交付。运行时本来就必须和程序放在一起
  （它要作为独立进程启动、往自己目录写配置），所以默认带上才是对的。
  打包脚本会在复制后**核验文件数与体积**，不一致就直接判失败。
- **onedir 而不是 onefile**：onefile 每次启动都要把 ~150MB 解压到临时目录，
  而这个应用要频繁重启（切账号、改端口），那个延迟很难受。而且 NapCat 运行时
  无论如何都得放在 exe 外面，onefile 省不掉"一个文件夹"这件事。
- **不用 UPX**：UPX 压过的 exe 更容易被杀软误报（需求规格 §9.4 已经提过这个风险）。
- 打包后**程序目录与用户数据目录分离**：即使装在 `Program Files` 这种只读位置，
  配置和记忆也照常落在 `%LOCALAPPDATA%\QLark\`。

> 打包完成后脚本会检查产物里是否真的带上了前端资源与 `schema.sql` ——
> 少了任何一个，程序会在启动时才崩，那种失败很难查。

> ⚠️ **请把产物放在可写的位置**（例如 `D:\QLark`、桌面、`%LOCALAPPDATA%`），
> **不要放进 `Program Files`**。QLark 自己把配置与记忆写在 `%LOCALAPPDATA%\QLark\`
> （程序目录只读也没关系），但**NapCat 会往自己目录里写** `config/`、`cache/`、
> `data/` —— 放在只读目录里它会写不进去。这是"免安装目录"这个形态的固有限制，
> 不是 bug；要做到"装进 Program Files 也能跑"，得在首启时把运行时复制到用户目录。

---

## 数据与配置

| 内容 | 位置 |
|---|---|
| 用户数据（配置、数据库、日志） | `%LOCALAPPDATA%\QLark\` |
| SQLite 数据库 | `%LOCALAPPDATA%\QLark\data\qlark.db` |
| 日志（按天分文件、按大小轮转） | `%LOCALAPPDATA%\QLark\logs\` |
| NapCat 运行时 | `<程序目录>\runtime\napcat\` |
| QQ 登录凭据（**不在 QLark 管辖内**） | `%USERPROFILE%\Documents\Tencent Files\<QQ号>\` |

> 程序目录与数据目录分离：即使程序装在只读位置（如 `Program Files`），配置与记忆也能正常落盘。

### 环境变量

| 变量 | 用途 |
|---|---|
| `QLARK_RUNTIME_DIR` / `QLARK_NAPCAT_DIR` | 覆盖 NapCat 运行时目录 |
| `QLARK_DATA_DIR` | 覆盖用户数据目录 |
| `QLARK_NAPCAT_EXE` / `QLARK_NAPCAT_ENTRY` | 覆盖启动可执行文件 / 入口脚本 |
| `QLARK_SKIP_ENV_CHECK=1` | 跳过环境自检门禁（**仅测试用**） |
| `QLARK_SHOWCASE` | 界面自动化脚本要切换到的视图 |

---

## 里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| **M1** | 进程管理 + 环境自检 + 登录状态机 + 界面外壳 + 仪表盘/日志页 | ✅ 已完成，**已于 2026-09-25 在真机 + 真实 NapCat 4.18.28 + 真实 QQ 账号上验收通过** |
| **M2** | 消息闭环：事件通道 + 消息段降级 + 触发/过滤/去重 + 会话串行队列 + 模型调用 + 回复 + 统计 | ✅ 已完成 |
| **M3** | 模型配置页 + 人设页 + `/person` 等指令 + 白名单权限 + 账号选择器 | ✅ 已完成 |
| **M4** | 记忆系统：合并式摘要 + 失败保护 + 记忆面板 | ✅ 已完成 |
| **M5** | 图片理解：立即下载 → 缩放 → base64 → 视觉模型 + 降级 | ✅ 已完成 |
| **M6** | 打包分发 + 首启向导 + 风控提示 | ✅ 已完成 |

> ⚠️ **M2 与 M3 曾在真机上完全不通**，却在单元/集成测试里全绿 —— 原因是测试替身
> （假 NapCat）比真身"更好说话"：两种通道名都跑得通、两种鉴权都认。
> 详见技术文档「坑 15」与 README 的排障小节。**凡是涉及引擎真实行为的假设，
> 替身必须照抄真身，且至少要真机验证一次。**

需求基线见 [`Readme/QLark-需求规格-v1.1-冻结版.md`](Readme/QLark-需求规格-v1.1-冻结版.md)，
事件通道与 M3 的补充见该文档的 v1.2 / v1.3 变更说明。

### M1 的已知取舍

- **关闭窗口 = 退出程序并停止 NapCat**（安全、可预期）。按需求规格 §3.1 应改为
  "最小化到托盘"，托盘图标与右键菜单待后续实现。
- 模型配置 / 人设配置 / 记忆面板为**占位页**，分别在 M3、M4 实现。
- 当前默认窗口 1180×780。在 150% 缩放的屏幕上视口约 1166 CSS px，仪表盘自动排成 2 列。
- **首次登录需要扫一次码**：客户端还不知道该用哪个 QQ。M3 会加「账号选择器」
  （列出本机检测到的已登录账号），届时可省掉这一次扫码。
