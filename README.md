# QLark 云雀

基于 [NapCat](https://github.com/NapNeko/NapCatQQ) 的 QQ 聊天机器人 **Windows 桌面客户端**。
适配 **OpenAI API 格式** 的 Key 调用，主打**多人设对话**与**记忆功能**，支持群聊与私聊。

- 语言：Python 3.13 ｜ 界面：pywebview（Edge WebView2）｜ 协议：OneBot 11
- 当前进度：**M1 骨架已完成**（进程管理 + 环境自检 + 登录状态机 + 界面外壳）

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
│   │   ├── store.py         记忆落库与人设读取
│   │   └── context.py       上下文组装
│   └── web/                 前端（纯 HTML/CSS/JS，无框架无外链）
├── tests/                   pytest（152 项）
├── tools/
│   ├── fetch_napcat.py      一键准备 NapCat 运行时（下载 + 解压 + 补 DLL + 校验）
│   ├── set_model.py         命令行设置模型配置（M3 的模型配置页之前的临时入口）
│   ├── fake_napcat.py       假 NapCat：无运行时也能端到端验证（含事件推送与消息注入）
│   ├── fake_openai.py       假 OpenAI 服务：无 API Key 也能端到端验证对话
│   ├── ui_check.py          界面布局自检：找出被裁切的内容、验证该滚动的能滚动
│   └── showcase.py          界面自动化：切到指定视图便于截图验证
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
| 回复形式 | 群聊默认引用原消息；超长回复自动按段落/句末标点分段 |
| 不支持的消息 | 语音 → `[语音消息]`、视频 → `[视频]`、表情 → `[表情]`、文件 → `[文件：名字]`… |
| 记忆 | 每个群一份、每个人设一份、不同群互不干扰；私聊按用户隔离；默认携带最近 20 轮 |
| 调用失败 | 可重试的错误自动退避重试；私聊会告知原因，**群聊不刷屏** |
| 一键暂停 | 仪表盘「运行信息」里的「暂停回复」按钮 |

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
| **M2** | 消息闭环：事件通道 + 消息段降级 + 触发/过滤/去重 + 会话串行队列 + 模型调用 + 回复 + 统计 | ✅ 已完成（已用假 NapCat + 假 OpenAI 端到端验证） |
| **M3** | 模型配置页 + 人设页 + `/person` 等指令 + 白名单权限 + 账号选择器 | ✅ 已完成（254 项测试全绿） |
| M4 | 记忆系统（SQLite + 合并式摘要 + 记忆面板） | 待开始 |
| M5 | 图片理解（立即下载 → 缩放 → base64） | 待开始 |
| M6 | 打包分发 + 首启向导 + 风控提示 | 待开始 |

需求基线见 [`Readme/QLark-需求规格-v1.1-冻结版.md`](Readme/QLark-需求规格-v1.1-冻结版.md)，
事件通道与 M3 的补充见该文档的 v1.2 / v1.3 变更说明。

### M1 的已知取舍

- **关闭窗口 = 退出程序并停止 NapCat**（安全、可预期）。按需求规格 §3.1 应改为
  "最小化到托盘"，托盘图标与右键菜单待后续实现。
- 模型配置 / 人设配置 / 记忆面板为**占位页**，分别在 M3、M4 实现。
- 当前默认窗口 1180×780。在 150% 缩放的屏幕上视口约 1166 CSS px，仪表盘自动排成 2 列。
- **首次登录需要扫一次码**：客户端还不知道该用哪个 QQ。M3 会加「账号选择器」
  （列出本机检测到的已登录账号），届时可省掉这一次扫码。
