# QLark 技术文档：NapCat 部署与 OneBot 链路搭建

> **文档版本**：v1.1 ｜ **编写时间**：2026-09-24 ｜ **对应 NapCat 版本**：4.18.28
>
> **v1.1 变更**：新增 7.4「登录界面集成类」坑、9.5「登录界面的实现」，均由一轮完整实测（故意退到扫码模式 → 再用 `-q` 登回）产出。
>
> **本文档用途**：完整记录 QLark 项目中 NapCat 的部署与链路打通全过程。
> 内容基于**实机验证**（非纯文档转述），可直接用于下一次对话中继续推进项目，或在一台全新机器上复现。


## 0. 如何使用本文档

1. 先读 **第 1~3 章** 建立背景认知（约 5 分钟）。
2. 复现环境按 **第 4 章** 逐步执行，每一步都有验收标准。
3. 遇到报错，直接查 **第 7 章「常见坑」**，按现象检索。
4. **第 8~9 章是全流程已实测验证的结论**——这是避免走弯路的关键，不要跳过。
5. 要做登录界面，直接看 **9.5 节**（含状态机、判据、静默启动要点、实验记录）；踩坑对照 **7.4 节**。
6. 项目还有本地记忆文件（见 10.4），与本文件互为补充。


## 1. 项目背景与目标

### 1.1 项目定位

QLark 是一个 **QQ 机器人的 Windows 桌面客户端**。用户双击即用，不接触命令行。

### 1.2 三条硬性约束

| # | 约束 | 说明 |
|---|---|---|
| 1 | **双击即开** | 不需要用户装 Python / Node / 手动开命令行 |
| 2 | **配置全在 GUI 内完成** | 用户不需要认识 JSON、不需要开浏览器进 WebUI |
| 3 | **不能有常驻黑窗口** | 客户端必须能静默拉起 NapCat 进程，全程无控制台窗口 |

### 1.3 当前阶段范围（重要）

- 本阶段**只做"打通链路"**：装好 NapCat、QQ 登录成功、OneBot 接口可调用。
- **不写业务代码**。客户端 UI 技术选型尚未开始。
- NapCat 作为**运行时依赖**放在 `runtime/napcat/`，**不纳入源码管理**。


## 2. 环境与前置条件

### 2.1 验证环境画像

| 项 | 值 |
|---|---|
| 操作系统 | Windows（win32），x64 / AMD64。本文命令均在 Windows 上验证 |
| 项目形态 | Python 工程，客户端 UI 技术栈未定 |
| 网络 | GitHub 直连可用；`ghfile.geekertao.top` 镜像更快（实测约 2 倍速） |
| 干扰项 | 实机装有第三方安全软件与网络代理工具，均未影响 NapCat 运行 |

### 2.2 路径书写约定（全文通用）

**本文不写死任何绝对路径**，统一使用以下占位符，便于在任何设备上复现：

| 占位符 | 含义 | 示例 |
|---|---|---|
| `<项目根>` | 项目仓库根目录 | 任意位置，如 `X:\code\QLark` |
| `<NapCat根>` | `<项目根>\runtime\napcat` —— NapCat 解压根目录 | — |
| `<QQ安装目录>` | 官方 QQ 的安装位置（**仅用于取补齐 DLL**） | winget 默认装到某盘 | 
| `%USERPROFILE%` | Windows 当前用户目录 | `C:\Users\<用户名>` |
| `<QQ号>` | 机器人 QQ 账号 | — |

> 下文所有 `runtime/...` 相对路径，均以 `<项目根>` 为基准。

### 2.3 版本矩阵（实测固化）

| 组件 | 版本 | 说明 |
|---|---|---|
| NapCat | **4.18.28** | 运行框架本体 |
| NapCat 内置 QQ 内核 | **9.9.31-49738** | 来自 `NapCat.Shell.Windows.Node.zip`，免装系统 QQ |
| 系统 QQ（winget 安装） | **9.9.33.52230** | **仅用于取补齐 DLL**，不参与运行 |
| 协议 | OneBot 11 | NapCat 的实现版本 |

> **注意**：`9.9.31` 的 `wrapper.node` 搭配 `9.9.33` 的 `ssl.dll` / `crypto.dll` **实测可正常工作**，版本不完全一致不影响。

### 2.4 目录约定

```
QLark/
├── runtime/                                # 运行时依赖，不纳入源码
│   ├── napcat/                             # NapCat 根目录（解压根目录）
│   │   ├── node.exe                        # 自带 Node 运行时（约 80 MB）
│   │   ├── index.js                        # 入口脚本 ★
│   │   ├── wrapper.node                    # QQ 内核桥接层（约 100 MB）
│   │   ├── crypto.dll / ssl.dll            # ★需手工补齐
│   │   ├── config.json / package.json      # QQ 内核版本声明
│   │   ├── napcat.bat                      # 内容仅一行：node.exe ./index.js
│   │   └── napcat/                         # NapCat 程序资源目录
│   │       ├── napcat.mjs                  # 主程序（约 3 MB）
│   │       ├── config/                     # ★所有配置在这里
│   │       │   ├── napcat.json             # 基础设置
│   │       │   ├── webui.json              # WebUI 设置（含静态 token）
│   │       │   └── onebot11_<QQ号>.json    # ★网络配置（按账号生成）
│   │       ├── cache/qrcode.png            # 当前登录二维码
│   │       ├── logs/                       # NapCat 自身日志
│   │       └── launcher.bat                # 注入式启动（本文不用，需管理员）
│   ├── _dl/                                # 下载缓存（可清理）
│   │   ├── Node.zip                        # 原始发行包
│   │   ├── vc_redist.x64.exe               # VC++ 运行库安装包
│   │   └── onekey-reference/               # 失效 OneKey 包的解包内容（仅存档参考）
│   └── 登录二维码.png                       # 便于查看的二维码副本
└── .workbuddy/memory/                      # 项目记忆文件
```

### 2.5 关键路径速查

下表路径均以 `<项目根>` 为基准，可直接照搬。

| 用途 | 路径 |
|---|---|
| NapCat 安装根目录 | `<项目根>\runtime\napcat` |
| 配置文件目录 | `<项目根>\runtime\napcat\napcat\config` |
| 启动命令（正式） | `cd <项目根>/runtime/napcat && node.exe ./index.js -q <QQ号>` |
| 二维码文件（仅首次登录用） | `<项目根>\runtime\napcat\napcat\cache\qrcode.png` |
| **登录凭据目录** | `%USERPROFILE%\Documents\Tencent Files\<QQ号>\` |


## 3. 整体架构

### 3.1 调用链

```
QQ 客户端 (NTQQ 内核)
        │  提供 SendMsg / 消息接收等原生接口
        ▼
   NapCat（猫猫框架，TypeScript）
        │  把这些能力翻译成 OneBot 11 规范的 HTTP / WebSocket
        ▼
   Bot 框架 / 业务程序（NoneBot、Koishi、自研客户端……）
```

**关键理解**：NapCat **不自己实现收发消息**，而是主动调用 QQ 客户端**已有的** Node 模块接口。所以它不需要 Hook、不依赖 Electron 注入，甚至能自己引导 QQ 内核启动。

### 3.2 四种网络模型

NapCat 对外只提供四种连接方式，**必须先选定一种**才能和业务程序通信：

| 模型 | 谁主动 | 通信方向 | 典型用途 |
|---|---|---|---|
| **HTTP 服务端** | 业务程序 → NapCat | 单工（收请求、回响应） | 最简单的调用方式，便于调试 ✅ 本文采用 |
| HTTP 客户端 | NapCat → 业务程序 | 单工（推事件） | 只需要接收事件上报 |
| **WS 服务端（正向 WS）** | 业务程序 → NapCat | 双工 | 主流 Bot 框架（NoneBot/Koishi 等） |
| **WS 客户端（反向 WS）** | NapCat → 业务程序 | 双工 | 最多框架的默认对接方式 |

> 定名规则容易混：**"正向/反向"是站在业务程序角度说的**。NapCat 文档里 "WS 服务端" = 正向 WS，"WS 客户端" = 反向 WS。
>
> **重要**：OneBot 的 HTTP 模型**不是** Webhook 回调，而是"业务程序主动 POST 到 NapCat 的接口地址"。事件上报才走 HTTP 客户端模式。

### 3.3 协议端默认状态

**全新安装的 NapCat，四种网络全部为空数组——一个端口都不监听。**
必须手动配置（见 4.7），这是第一次接入时最容易忽略的一步。


## 4. 搭建流程

### 步骤 1：选对发行包（重要分支）

NapCat 有多个发行包，**选错会浪费大量时间**：

| 包名 | 大小 | 是否采用 | 说明 |
|---|---|---|---|
| `NapCat.Shell.Windows.Node.zip` | ~111 MB | ✅ **采用** | **自带 QQ 内核 + Node.js**，解压即用，**免装系统 QQ** |
| `NapCat.Shell.zip` | 小 | ❌ | 需要用户自行安装系统 QQ |
| `NapCat.Shell.Windows.OneKey.zip` | ~1 MB | ❌ **已失效** | 引导器，内置 QQ 下载地址已 404 |
| `NapCat.Framework.zip` | 小 | ❌ | LiteLoaderQQNT 插件形态，官方自 QQ 9.9.19 后已不推荐 |

**决策理由**：Node 包是唯一满足"双击即开"约束的形态——QQ 内核、Node 运行时、NapCat 三者一体，用户机器上不需要任何前置依赖。

> **Framework 版为什么不选**：官方明确鼓励迁移到 Shell。且自 QQ 9.9.19 后 LiteLoaderQQNT 维护欠缺，使用其官方修补方案会导致 NapCat 扩展 API 失效、污染 QQ 环境、追加环境变量。

#### 下载命令

```bash
# 在 <项目根> 下建下载缓存目录
cd <项目根>/runtime
mkdir -p _dl && cd _dl

# 方式 A：GitHub 直连
curl -L -o Node.zip \
  https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.28/NapCat.Shell.Windows.Node.zip

# 方式 B：镜像加速（实测约 2 倍速，推荐）
curl -L -o Node.zip \
  https://ghfile.geekertao.top/https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.28/NapCat.Shell.Windows.Node.zip
```

> Releases 页面：<https://github.com/NapNeko/NapCatQQ/releases>
> tag 命名规则为 `v<版本号>`，若 `v4.18.28` 不存在请以页面实际 tag 为准。

**验收**：`Node.zip` 大小约 111 MB（不是 1 MB——若只有 1 MB 说明下成了 OneKey 版）。

### 步骤 2：解压

```bash
cd <项目根>/runtime
mkdir -p napcat
# 将 Node.zip 解压到 napcat/ ，使 node.exe 直接位于 napcat/ 下
unzip -o _dl/Node.zip -d napcat
```

**验收**：`runtime/napcat/node.exe`、`runtime/napcat/index.js`、`runtime/napcat/wrapper.node` 三者均存在。

> ⚠️ 注意 zip 内可能多套一层目录，要确保 `node.exe` 就在 `runtime/napcat/` 直下，而不是 `runtime/napcat/某层/node.exe`。

### 步骤 3：安装 VC++ 运行库

**这是必须先做的一步**，否则后面 `wrapper.node` 会加载失败。

```bash
cd <项目根>/runtime/_dl
curl -L -o vc_redist.x64.exe https://aka.ms/vs/17/release/vc_redist.x64.exe
./vc_redist.x64.exe /install /quiet /norestart
```

**验收**：`C:\Windows\System32` 下出现 `vcruntime140.dll`、`vcruntime140_1.dll`、`msvcp140.dll`。

> 实测：**新装的 Windows 上这三个文件通常是缺失的**，必须先装。
> 若安装程序需要管理员权限，需以管理员身份运行。

### 步骤 4：补齐 `crypto.dll` / `ssl.dll`（关键坑）

`wrapper.node` 的 PE 导入表依赖 `crypto.dll` 和 `ssl.dll`，但**官方 Node 包中根本没有这两个文件**（已核对 zip 清单，非杀软误删），属上游打包缺陷。

**修复方法**：装一个官方 QQ 取文件，然后拷过来。

```bash
# 1) 用 winget 安装官方 QQ（不要用 NapCat 官方脚本里的直链，已全部 404）
winget install --id Tencent.QQ.NT --exact
# 实测版本 9.9.33.52230

# 2) 查安装目录（winget 默认位置因机器而异，不要假设在某盘）
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ" /v UninstallString
# 由输出可反推安装根目录；app 资源目录位于：
#   <QQ安装目录>\versions\<版本号>\resources\app\

# 3) 拷贝两个 DLL 到 NapCat 根目录
cp "<QQ安装目录>/versions/<版本号>/resources/app/crypto.dll" <项目根>/runtime/napcat/
cp "<QQ安装目录>/versions/<版本号>/resources/app/ssl.dll"    <项目根>/runtime/napcat/
```

**验收**：`runtime/napcat/crypto.dll`（约 1.5 MB）、`runtime/napcat/ssl.dll`（约 0.5 MB）存在。

> **不需要**登录这个 QQ，装完取文件即可。
> 诊断技巧：用 Python 解析 `.node` / `.dll` 的 PE 导入表，可以精确定位缺失依赖；GitHub Release 资源支持 `Range` 请求，可远程读 zip 中央目录而无需整包下载。

### 步骤 5：首次启动

```bash
cd <项目根>/runtime/napcat
node.exe ./index.js
```

**要点**：

- 启动命令**本质就是 `node.exe ./index.js`**（`napcat.bat` 的全部内容就是这一行）。
- **不需要管理员权限**，**不需要**已安装的系统 QQ。
- 这**不是**注入式启动——`napcat/launcher.bat`（用 `NapCatWinBootMain.exe` 注入）是另一条路，需要管理员权限和已安装的 QQ，**本项目不使用**。
- 客户端集成时，这一行可以**直接静默 spawn**，天然无窗口，正好满足约束 3。

**验收**：日志出现以下内容

```
[NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=<token>
[NapCat.Core] Version: 4.18.28
```

首次启动会生成 `napcat/config/webui.json`。

> **建议**：正式排障时把 `napcat/config/napcat.json` 的 `fileLog` 改为 `true`，否则只有控制台输出（默认 `false`）。

### 步骤 6：QQ 登录

**方式 A：WebUI（推荐）**

浏览器打开启动日志里打印的地址：

```
http://127.0.0.1:6099/webui?token=<webui.json 中的 token>
```

进入后选"二维码登录"，用手机 QQ 扫码。WebUI 会自己刷新二维码，比截图可靠。

**方式 B：控制台二维码**

日志里会以字符画形式打印二维码，同时**落盘为真实 PNG**：

```
runtime/napcat/napcat/cache/qrcode.png
```

以及日志中的解码 URL，形如 `https://txz.qq.com/p?k=<...>&f=<1600001604>`，可自行生成二维码。

**方式 C：快速登录（`-q` 参数）✅ 已实测验证**

```bash
node.exe ./index.js -q <QQ号>
# 实测：node.exe ./index.js -q 2157649443
```

**这是产品化唯一可用的登录方式**——用户无需扫码，直接进入已登录状态。

成功的日志特征：

```
[info] 正在快速登录  2157649443
[info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略
```

失败（未带 `-q`，回退二维码）的日志特征：

```
[info] 没有 -q 指令指定快速登录，将使用二维码登录方式
[info] 可用于快速登录 of QQ：
1. 2658495997 毛糙蛋
2. 2157649443 米蛋
[warn] 请扫描下面的二维码，然后在手Q上授权登录：
```

**快速登录的三种入口**（源码反编译得出）：

| 入口 | 形式 | 说明 |
|---|---|---|
| 启动参数 | `-q <uin>` | 最直接，推荐 |
| 环境变量 | `NAPCAT_QUICK_ACCOUNT` | 适合 spawn 时注入 |
| 密码兜底 | `NAPCAT_QUICK_PASSWORD` / `NAPCAT_QUICK_PASSWORD_MD5` | 无历史登录记录时使用 |

**判定逻辑**：`-q` 指定账号 → 查历史登录记录 → 有则快速登录；无则尝试密码登录；再失败才输出二维码。

**登录凭据的落点**：

| 路径 | 内容 |
|---|---|
| `%USERPROFILE%\Documents\Tencent Files\<QQ号>\` | ★ **账号数据**，`-q` 快速登录读的就是这里 |
| `%USERPROFILE%\Documents\Tencent Files\NapCat\data` | NapCat 自身数据/缓存 |

> **两个反直觉点（都经实测确认）**
>
> **① 别按传统 QQ 的路径找账号数据。**
> NapCat 的 QQ 内核是 **pure shell 模式**（`qqnt.json` 中 `isPureShell: true`），没有 Electron 渲染层，
> 因此**不会**在 `AppData\Roaming\QQ\Partitions` 下生成 `qqnt_<QQ号>` 分区目录。
> 认准上表的 `Documents\Tencent Files\<QQ号>\`。
>
> **② 启动日志里的 `可用于快速登录 of QQ：` 候选列表不可靠，不要拿它做判据。**
> 实测：经历一次成功扫码 + 两次成功快速登录后，列表里**能看到** NapCat 自己登录过的账号；
> 但在一次 `taskkill /F` 硬杀之后紧接着启动时，该列表**是空的**。
> → 客户端必须**自己持久化上次成功的 QQ 号**，直接用 `-q` 指定；失败再降级到扫码。

> 相关示例脚本见 `napcat/quickLoginExample.bat`。

**登录成功的标志**：

1. `napcat/config/` 下**新增三个文件**，文件名带你的 QQ 号：
   - `napcat_<QQ号>.json`
   - `onebot11_<QQ号>.json` ← **网络配置在这里**
   - `napcat_protocol_<QQ号>.json`
2. 日志开始出现 `接收 <- 群聊 [...]` / `发送 -> ...` 记录。

**实测结果**：QQ `2157649443`（昵称"米蛋"）登录成功。

### 步骤 7：配置 OneBot 网络

协议端默认**一个端口都不开**（见 3.3），必须手动配置。

#### 方式 A：WebUI（人工推荐）

WebUI → 网络配置 → 新建 → 选择类型 → 填参数 → **勾选"保存时启用"** → 保存。
WebUI 的配置是**即时生效**的。

#### 方式 B：直接改配置文件（程序化必需）

编辑 `napcat/config/onebot11_<QQ号>.json`。

**⚠️ 关键限制：改配置文件不会热重载，必须重启 NapCat 才生效。**（实测结论，见 8.1）

以下为**本项目实测使用**的 HTTP 服务端配置：

```json
{
  "network": {
    "httpServers": [
      {
        "name": "httpServer",
        "enable": true,
        "port": 3000,
        "host": "127.0.0.1",
        "enableCors": true,
        "enableWebsocket": true,
        "messagePostFormat": "array",
        "token": "qlark-local-verify",
        "debug": false,
        "reportSelfMessage": false
      }
    ],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "imageDownloadProxy": "",
  "timeout": {
    "baseTimeout": 10000,
    "uploadSpeedKBps": 256,
    "downloadSpeedKBps": 256,
    "maxTimeout": 1800000
  }
}
```

> **安全提示**：`host` 填 `127.0.0.1` 而非 `0.0.0.0`，确保只监听本机。
> **`token` 必须设置**——服务端模式下没有 token 等于把 QQ 账号交出去。
> `enableWebsocket: true` 让同一个端口同时支持 HTTP 和 WS 升级。

**其他三种模型的配置骨架**（按需替换对应数组，其余留空即可）：

```jsonc
// HTTP 客户端（NapCat 主动上报事件到你的服务）
"httpClients": [{
  "name": "httpClient", "enable": true,
  "url": "http://localhost:8080",
  "messagePostFormat": "array", "reportSelfMessage": false,
  "token": "", "debug": false
}]

// WS 服务端（正向 WS）
"websocketServers": [{
  "name": "WsServer", "enable": true,
  "host": "0.0.0.0", "port": 3001,
  "messagePostFormat": "array", "reportSelfMessage": false,
  "token": "", "debug": false, "heartInterval": 30000
}]

// WS 客户端（反向 WS）
"websocketClients": [{
  "name": "WsClient", "enable": true,
  "url": "ws://localhost:8082",
  "messagePostFormat": "array", "reportSelfMessage": false,
  "reconnectInterval": 5000, "token": "", "debug": false,
  "heartInterval": 30000
}]
```

> `messagePostFormat`：`array` 为消息段数组，`string` 为 CQ 码字符串。
> 自 v4.4.14 起 NapCat 用 JSON5 解析请求，配置里可写注释和尾随逗号（但低版本 v4.4.7 之前严禁写注释）。

**重启并验收**：

```bash
# 彻底重启（见步骤 8 的进程管理注意事项）
# 重启后登录，日志应出现：
#   [AdapterManager] OneBot11 适配器初始化完成
#   [OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3000
```

### 步骤 8：验证链路

```bash
B="http://127.0.0.1:3000"; T="qlark-local-verify"

curl -s -X POST "$B/get_login_info" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $T" -d '{}'

curl -s -X POST "$B/get_status" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $T" -d '{}'
```

**实测验收结果**：

| 接口 | 返回 |
|---|---|
| `get_login_info` | `{"user_id":2157649443,"nickname":"米蛋"}` |
| `get_status` | `{"online":true,"good":true,"stat":{}}` |
| `get_version_info` | `{"app_name":"NapCat.Onebot","protocol_version":"v11","app_version":"4.18.28"}` |
| `get_group_list` | 15 条 |
| `get_friend_list` | 203 条 |
| 无 token 调用 | `{"message":"token verify failed!"}` ✅ 鉴权生效 |


## 5. 配置文件详解

所有配置文件位于 `runtime/napcat/napcat/config/`。

### 5.1 `napcat.json` — 基础设置

```jsonc
{
  "fileLog": false,          // ★是否写文件日志（默认 false，排障时改 true）
  "consoleLog": true,
  "fileLogLevel": "debug",
  "consoleLogLevel": "info",
  "packetBackend": "auto",   // PacketBackend 实现，多平台已内置，保持 auto
  "packetServer": "",
  "o3HookMode": 1,           // 遇社交风控可尝试设为 0 关闭包拦截
  "bypass": { "hook": false, "window": false, "module": false,
              "process": false, "container": false, "js": false }
}
```

### 5.2 `webui.json` — WebUI 设置

```jsonc
{
  "host": "::",                 // 实测为双栈（IPv4 + IPv6）
  "port": 6099,                 // 端口设为 0 即完全禁用 WebUI
  "token": "9c145eb32343",      // 静态 token（仅供人工访问 WebUI）
  "loginRate": 10,              // 每分钟登录次数限制
  "autoLoginAccount": "",       // WebUI 侧的自动登录账号（与 -q 快速登录是两套机制，此处可留空）
  "disableWebUI": false,
  "accessControlMode": "none",
  "ipWhitelist": [], "ipBlacklist": [],
  "enable2FA": false, "totpSecret": ""
}
```

**运行时规律**：

- 端口被占用时会**自动 +1**，最多尝试 100 次，失败则禁用 WebUI。实际端口以启动日志为准。
- 二维码登录后会**刷新 WebUI Token**，并可能强制要求修改密码。

### 5.3 `onebot11_<QQ号>.json` — 网络配置

见步骤 7。这是**唯一需要程序化读写的配置**。

### 5.4 `config.json` / `qqnt.json` — 内核版本声明（只读了解）

- `config.json`：`baseVersion` / `curVersion` = `9.9.31-49738`，`buildId` = `49738`
- `qqnt.json`：QQ 内核的 `package.json` 覆盖层，`main` 指向 `./loadNapCat.js`

> **不要手工改这两个文件**，它们由 NapCat 管理。

### 5.5 启动入口 `index.js`

```js
const path = require('path');
const { pathToFileURL } = require('url');

const BASE_DIR = __dirname;
process.env.NAPCAT_WRAPPER_PATH = path.join(BASE_DIR, 'wrapper.node');
process.env.NAPCAT_QQ_PACKAGE_INFO_PATH = path.join(BASE_DIR, 'package.json');
process.env.NAPCAT_QQ_VERSION_CONFIG_PATH = path.join(BASE_DIR, 'config.json');
process.env.NAPCAT_DISABLE_PIPE = '1';
import(pathToFileURL(path.join(BASE_DIR, 'napcat', 'napcat.mjs')).href);
```

**理解要点**：入口只是**设置 4 个环境变量**然后 import 主程序。客户端集成时，最干净的做法是**照抄这个模式**：自己 spawn `node.exe`，通过环境变量传路径，而不是依赖 `napcat.bat`。


## 6. OneBot 接口使用

### 6.1 调用约定（实测固化）

```
POST http://127.0.0.1:3000/<action>
Content-Type: application/json
Authorization: Bearer <token>

{"参数名": "值"}     // 无参数时传 {}
```

### 6.2 响应结构

```json
{
  "status": "ok",              // ok / failed
  "retcode": 0,                // 0 = 成功
  "data": { },                 // 业务数据
  "message": "",
  "wording": "",
  "echo": "8l9z4uf8889",       // 回显，与请求 echo 对应
  "stream": "normal-action"    // ⚠️ 非标准字段
}
```

> **注意 `stream` 字段**：这是 NapCat v4.8.115+ 引入 Stream API 后新增的，
> 普通接口值为 `"normal-action"`，流式接口为 `"stream-action"`。
> **它不是 OneBot 标准字段，写解析代码时不要当成异常。**

### 6.3 接口覆盖面

NapCat 实现了约 **106 个** API，几乎全部可用：

| 分类 | 数量 | 代表 |
|---|---|---|
| OneBot 11 标准 | 35 | `send_msg`、`get_group_list`、`set_group_ban` |
| go-cqhttp 兼容 | 32 | `get_group_msg_history`、`upload_group_file`、`ocr_image` |
| NapCat 扩展 | 39 | `nc_get_rkey`、`get_mini_app_ark`、`send_poke` |

**完整 API 文档**：<https://napcat.apifox.cn>


## 7. 常见坑

### 7.1 环境类

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 1 | `Error: The specified module could not be found. ...\wrapper.node` | 缺 VC++ 运行库 | 装 `vc_redist.x64.exe`（步骤 3） |
| 2 | 同上，但 VC++ 已装 | 官方 Node 包**缺 `crypto.dll` / `ssl.dll`**（上游打包缺陷） | 从 `<QQ安装目录>\versions\<ver>\resources\app\` 拷两个 DLL 到 NapCat 根目录（步骤 4） |
| 3 | OneKey 版安装器卡住 / 报 404 | `OneKey.zip` 内置的 QQ 下载直链已全部失效 | **不要用 OneKey**，改用 `Shell.Windows.Node.zip` |
| 4 | 官方 `install.ps1` 下载 QQ 失败 | `dldir1.qq.com/qqfile/qq/QQNT/<hash>/` 路径**已整体下架** | 用 `winget install --id Tencent.QQ.NT --exact` |
| 5 | 找不到 QQ 安装包下载地址 | QQ CDN 路径已变更 | 新规律：`https://qqdl.gtimg.cn/qqfile/QQNT/<版本>/release/<hash>/QQ_<版本>_<日期>_x64_01.exe` |
| 6 | macOS 上音频处理失败 | 系统沙盒机制禁止运行外部程序 | macOS **不支持外部 FFmpeg**，属已知限制 |

### 7.2 行为类（更隐蔽，务必看完）

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 7 | **改了 `onebot11_*.json`，端口起不来** | **配置文件不支持热重载** | 必须重启 NapCat（见 8.1、9.1） |
| 8 | **WebUI 的 API 用静态 token 调不通** | 内部鉴权是**基于时间的 HMAC 动态密钥**，`webui.json` 的静态 token 无效（raw / sha256 都返回 `Unauthorized`，传 body 返回 `token is empty`） | **不要走 WebUI API**，改为直接读写 `config/*.json`（见 9.1） |
| 9 | **重启后又要重新扫码** | 启动时**没带 `-q <QQ号>`**，NapCat 默认走二维码登录 | 启动命令加上 `-q <QQ号>`（见步骤 6 方式 C）。**与是否优雅退出无关** |
| 10 | 杀掉了 NapCat 进程，但它"自己又活了" | NapCat 有**自愈机制**：杀掉 worker 子进程后，父进程会自动重新拉起一个新的 worker | 要彻底重启**必须杀父进程树**（`taskkill /PID <父PID> /T /F`；注意 `/T` 只杀子树，要对父进程用才有效） |
| 11 | 新装的 NapCat 调接口全部连不上 | **协议端默认不开任何端口** | 手动配置网络（步骤 7） |
| 12 | 服务端模式暴露公网后账号异常 | 未配 token | **必须配 token**；`host` 优先填 `127.0.0.1` |
| 13 | v4.8.106 以下 WebUI 被爆破 | 默认弱密码 `napcat` | 升级到 v4.8.106+（改用启动随机 Token），或立即改密码/关闭 WebUI |

### 7.3 数据与资源限制（设计时必须考虑）

| # | 限制 | 影响 |
|---|---|---|
| 14 | **NapCat 无数据库**，消息走 LRU 缓存，**约 5000 条后过期** | 需要消息留存必须**自己落库** |
| 15 | **已撤回的消息无法恢复** | 撤回事件只能拿到事件本身，拿不回原消息内容 |
| 16 | **消息 ID 是基于哈希的正整数，非连续** | 不能当作自增 ID 用，也不能靠它推断消息顺序 |
| 17 | **图片 URL 约 2 小时过期**（报 `url expired`） | 用 `nc_get_rkey` 刷新 rkey，或重新 `get_image` / `get_file` / `get_msg` |
| 18 | **视频上限 100 MB** | 超出必须走群文件方式 |
| 19 | **音频是腾讯 silk 格式** | 收发都需 silk + ffmpeg 转码；v4.4.11+ 除手动装的 Linux 外默认免配 |
| 20 | 大文件 / 跨设备传输 | 需 v4.8.115+ 的 Stream API（action 名必须以 `stream` 结尾） |

### 7.4 登录界面集成类（做 GUI 时必看，全部实测确认）

| # | 现象 | 原因 | 解决 |
|---|---|---|---|
| 21 | **已登录了，界面却显示一个扫不动的二维码** | `cache/qrcode.png` **登录成功后不会被删除**，会残留上次的图。<br>实测：16:24 生成的码，16:25 快速登录成功后文件**仍在原地，mtime 未变** | 启动 NapCat **前先删除该文件**；之后以「文件出现 **且** mtime > 进程启动时间」为判据 |
| 22 | 用户扫了码却提示失效 | 二维码**每约 121 秒自动覆盖重写同名文件**。<br>实测：16:22:21 → 16:24:22，大小 594 → 599 B | 监听文件 mtime 变化并刷新界面，**不能静态展示** |
| 23 | NapCat 进程莫名卡住、不响应 | 启动输出（含大幅二维码字符画）**没有被持续读取**，管道缓冲区写满后进程阻塞 | 启动后必须持续消费 stdout（独立读线程，或重定向到文件并持续读） |
| 24 | 拿候选账号去快速登录却失败 | `可用于快速登录 of QQ：` 列表**内容会浮动**：硬杀后为空，正常登录后才有 | 不要依赖它；**客户端自己持久化 QQ 号** |
| 25 | 快速登录"突然不能用了" | 凭据失效：改密码 / 异地登录 / 风控 / 长期未登录 | 必须实现**降级到扫码**的路径，不能假设快速登录永远有效 |


## 8. 已验证结论与待办事项

### 8.1 已实测验证 ✅

| 结论 | 证据 |
|---|---|
| `NapCat.Shell.Windows.Node.zip` + VC++ + 补两个 DLL = 可运行 | 登录成功，日志 `NapCat.Core Version: 4.18.28` |
| `node.exe ./index.js` 免管理员、免系统 QQ、无窗口 | 实测直接跑通 |
| **改 `config/*.json` + 重启 → 配置生效** | 重启后 `[OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3000` |
| 配置文件协议端默认全空 | `onebot11_*.json` 初始四数组皆空 |
| WebUI API 无法程序化调用 | `/api/auth/login` 三种方式均失败 |
| **配置文件不支持热重载** | 写入后等待，端口未监听 |
| NapCat 自愈机制存在 | 杀掉 worker 子进程后，父进程自动拉起新 worker |
| **`-q <QQ号>` 快速登录可用，连续多次重启均免扫码** | 16:05:43 / 16:06:30 / 16:25:41 三次均为 `正在快速登录 2157649443` → `Worker进程已登录成功`，全程无二维码 |
| 二维码**每约 121 秒覆盖重写**同名文件 | 16:22:21 → 16:24:22，文件大小 594 → 599 B |
| **`cache/qrcode.png` 登录成功后不清理，会残留旧图** | 16:24:22 生成的码，在 16:25:41 快速登录成功、端口就绪后**仍在原地且 mtime 未变** |
| `可用于快速登录 of QQ：` 候选列表**内容浮动，不可依赖** | 硬杀后启动时该列表为空；正常登录后同一位置列出两个账号（含 NapCat 自己登录的 2157649443） |

**如何自行观察自愈机制**：先用 `netstat -ano | findstr :3000` 记下监听端口的 PID，
再用 `taskkill /PID <该PID> /T /F` 杀掉，几秒后重新 `netstat` 会看到端口被**新的 PID** 接管 —— 但此时 NapCat 已退化为未登录状态。

### 8.2 快速登录：免扫码重启（已验证 ✅ 核心结论）

> **结论：只要启动命令带 `-q <QQ号>`，关闭/重开 NapCat 全程免扫码。**
> **并且与"是否优雅退出"无关——`taskkill /F` 硬杀之后，带 `-q` 重启照样免扫码登录成功。**

这是本阶段**最重要的发现**，直接决定约束 1「双击即开」成立。

**验证方法**：杀掉 NapCat 父进程（`taskkill /PID <父PID> /T /F`），然后执行

```bash
node.exe ./index.js -q 2157649443
```

**两次实验的实测日志**：

```
16:05:43 [info] 正在快速登录  2157649443
16:05:43 [info] [NapCat] [Fork] Worker进程已登录成功，切换到正常重试策略
```
```
16:06:30 [info] 正在快速登录  2157649443
16:06:31 [info] [NapCat] [Fork] Worker进程已登录成功
```

两次均**无二维码**，`127.0.0.1:3000` 自动监听，`get_login_info` 正常返回 `2157649443 / 米蛋`。

**为什么能免扫码**：登录凭据落在账号数据目录

```
%USERPROFILE%\Documents\Tencent Files\<QQ号>\
```

该目录**独立于 NapCat 程序目录**，因此：
- 杀掉进程、重启程序 → 不影响凭据
- 升级/重装 `runtime/napcat/` → 不影响凭据

> 另有一个 NapCat 自身的数据目录 `%USERPROFILE%\Documents\Tencent Files\NapCat\data`，与凭据分开。

**配套的环境变量入口**（spawn 时比传参更优雅）：

| 变量 | 作用 |
|---|---|
| `NAPCAT_QUICK_ACCOUNT` | 指定快速登录账号，等价于 `-q` |
| `NAPCAT_QUICK_PASSWORD` | 密码兜底（无历史记录时） |
| `NAPCAT_QUICK_PASSWORD_MD5` | 同上，MD5 形式 |

### 8.3 其他未决事项

- QLark 客户端的 UI 技术选型（仓库仍是 PyCharm 空工程）
- 客户端与 NapCat 的网络拓扑最终选型（HTTP 服务端 vs 反向 WS）
- 消息持久化方案（因为 NapCat 无数据库，见坑 14）


## 9. 面向客户端产品的关键结论

这一章是给 QLark 客户端设计用的**设计输入**，来自本轮实测。

### 9.1 客户端应当绕开 WebUI，直接管配置文件

WebUI 的内部 API 是**基于时间的 HMAC 动态密钥**，静态 token 调不通（坑 8）。
好消息是**不需要它**——已验证「改 `config/*.json` + 重启」这条路径完全可行。

**规定动作**：

1. 读取 `config/onebot11_<QQ号>.json`（按账号文件名匹配）
2. 用 JSON 合并的方式写入 `network` 段，**不要整体覆盖**（避免丢掉用户其他配置）
3. 写前备份
4. 重启 NapCat 进程

WebUI 只作为**人工兜底工具**保留，不作为程序化接口。

### 9.2 启动必须带 `-q`；退出不必追求优雅

**这是从 8.2 得出的两条结论，能显著简化客户端实现。**

**① 启动命令必须带 `-q <QQ号>`**（或在 spawn 时注入 `NAPCAT_QUICK_ACCOUNT`）。

不带 `-q` 就是二维码登录，用户每次都得扫码——约束 1「双击即开」立刻不成立。

**② 退出方式不必追求优雅。**

实测硬杀（`taskkill /F`）之后，带 `-q` 重启**依然免扫码成功**。因此：

- ❌ 不需要为"优雅关闭 NapCat"做额外设计
- ❌ 不需要等待落盘时间
- ✅ 直接终止进程即可，行为等价

> ⚠️ **唯一例外**：用户在客户端里**切换 QQ 账号**时，新账号没有登录凭据，需要重新扫码一次。

### 9.3 进程管理的三个硬事实

| 事实 | 设计影响 |
|---|---|
| 无窗口启动 | `node.exe ./index.js` 天然无控制台，可直接静默 spawn ✅ |
| 有自愈机制 | 杀子进程会被自动重拉，**必须管理父进程**（坑 10） |
| 需要先登录才开端口 | 未登录时网络配置不生效，客户端启动流程应是「拉起 → 判断登录态 → 未登录则引导扫码 → 登录后配置网络」 |

### 9.4 客户端启动流程建议

```
1. 首次运行环境自检：VC++ 运行库 / crypto.dll / ssl.dll 是否就绪
   └─ 缺失则自动补齐（VC++ 静默安装；DLL 从内置副本拷贝）
2. 读取本地保存的 QQ 号（首次运行没有 → 引导用户扫码一次）
3. spawn  node.exe ./index.js -q <QQ号>
   ├─ 成功 → 进入 4
   └─ 失败（日志出现"没有 -q 指令…"或输出二维码）→ 引导用户扫码
4. 轮询 config/napcat_<QQ号>.json 出现 → 确认登录成功
5. 检查 onebot11_<QQ号>.json 的 network 段
   ├─ 已配置 → 直接使用
   └─ 未配置 → 按模板合并写入 → 重启一次
6. 轮询目标端口（如 3000）就绪 → 对外提供服务
7. 退出时：直接终止进程树即可（无需优雅等待，见 9.2）
```

**关键实现要点**：

| 要点 | 做法 |
|---|---|
| 判重，避免重复写配置 | 先读 `onebot11_<QQ号>.json`，判断 `network` 段是否已有启用项 |
| **合并而非覆盖** | 只改 `network` 子段，保留 `timeout`、`musicSignUrl` 等其他字段 |
| 父进程管理 | 停进程要杀**父进程树**（`taskkill /PID <父PID> /T /F`）；只杀子进程会被自愈机制重拉 |
| 工作目录 | 必须以 `runtime/napcat/` 为工作目录，否则 `index.js` 的路径推导会出错 |
| 日志 | 把 `napcat.json` 的 `fileLog` 设为 `true`，并重定向 stdout，便于 GUI 展示运行状态 |
| 登录态持久化位置 | `%USERPROFILE%\Documents\Tencent Files\<QQ号>\`（**与程序目录分离**，重装 NapCat 不丢） |


### 9.5 登录界面的实现（已实测）

对应 9.4 流程里的第 2~4 步。核心只有两件事：**读一个 PNG** 和 **带一个参数重启**。难点全在细节（见 7.4）。

#### ① 显示二维码

```
启动前：删除 cache/qrcode.png            ← 否则会显示上次的过期图（坑 21）
启动后：轮询该文件
        文件出现 且 mtime > 进程启动时间  → 加载显示
        mtime 发生变化                    → 重新加载（约 121 秒一次，坑 22）
登录成功                                  → 隐藏
```

它是标准 PNG，直接交给 UI 框架渲染，不需要解码、不需要调接口。

#### ② 判断当前处于哪种登录模式（读启动输出，两种特征互斥）

| 模式 | 输出特征 |
|---|---|
| 快速登录 | `正在快速登录  <QQ号>` 紧接着 `Worker进程已登录成功` |
| 扫码登录 | `没有 -q 指令指定快速登录，将使用二维码登录方式` + 二维码字符画 |

#### ③ 判断登录完成（两个独立信号，建议都实现）

- 输出出现 `Worker进程已登录成功`
- 文件 `config/napcat_<QQ号>.json` 出现

#### ④ 快速登录按钮的逻辑

```
有已保存的 QQ 号 → 用 -q <QQ号> 重启 NapCat
                   失败（输出落到"将使用二维码登录方式"）→ 自动切到扫码界面
无已保存的 QQ 号 → 直接进扫码界面
登录成功         → 把 QQ 号存进客户端自己的配置
```

#### ⑤ Windows 静默启动要点

| 要点 | 做法 |
|---|---|
| 不弹黑窗 | Python `subprocess.Popen(..., creationflags=CREATE_NO_WINDOW)`；Node `spawn(..., { windowsHide: true })` |
| 不阻塞 | 必须**持续读 stdout**（坑 23） |
| 工作目录 | 必须设为 `<项目根>\runtime\napcat` |
| 停止 | 杀**父进程树**（`taskkill /PID <父PID> /T /F`）；只杀 worker 会被自愈机制重拉 |

#### ⑥ 本次实验的完整记录

| 步骤 | 结果 |
|---|---|
| 清空 `qrcode.png`，**不带 `-q`** 启动 | 16:22:21 生成二维码；日志含 `将使用二维码登录方式` ✅ |
| 等待一个刷新周期 | 16:22:21 → 16:24:22 文件被覆盖重写，间隔 **121 秒** ✅ |
| 停掉 NapCat 进程 | 二维码文件**依然残留**，mtime 未变 ✅ |
| **带 `-q <QQ号>`** 重启 | `正在快速登录` → `Worker进程已登录成功`，**全程无二维码** ✅ |
| 再查二维码文件 | **仍是 16:24:22 的旧图**，未重新生成 ✅ ← 坑 21 的现场复现 |
| 接口验证 | `get_login_info` 正常返回，3000 端口自动监听 ✅ |

> **结论：「GUI 显示二维码 + 快速登录按钮」技术上没有难点**，风险全部集中在上面的工程细节里。


## 10. 附录

### 10.1 一次成功搭建的「参考特征」

搭好之后应当能观察到以下现象。若某条对不上，就按第 7 章排查。

| 检查点 | 应看到的结果 |
|---|---|
| 进程 | `node.exe` 存活，且监听 **6099**（WebUI）与 **3000**（OneBot）两个端口 |
| 启动日志 | `[NapCat.Core] Version: 4.18.28`、`Worker进程已登录成功` |
| 网络日志 | `[OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3000` |
| 配置目录 | `config/` 下出现 `napcat_<QQ号>.json`、`onebot11_<QQ号>.json`、`napcat_protocol_<QQ号>.json` 三个文件 |
| 接口自测 | `get_login_info` 返回 `status: ok` 与正确的 `user_id` / `nickname` |
| 鉴权自测 | 不带 token 调用返回 `{"message":"token verify failed!"}` |

**常用排查命令**：

```bash
netstat -ano | findstr ":6099"      # WebUI 是否在监听
netstat -ano | findstr ":3000"      # OneBot 端点是否在监听
tasklist  | findstr node.exe        # NapCat 进程是否存活
```

> **日志对照**：快速登录的判定行是 `正在快速登录  <QQ号>`；
> 落到二维码则是 `没有 -q 指令指定快速登录，将使用二维码登录方式`。
> 若启动时把输出重定向到文件（`> napcat.log 2>&1`），每重启一次建议换个文件名以便对照。

### 10.2 配置文件备份

改 `onebot11_<QQ号>.json` 前先备份：

```bash
cp onebot11_<QQ号>.json onebot11_<QQ号>.json.bak
```

保留原始的空网络配置以便回滚。

### 10.3 可清理项

| 路径 | 说明 |
|---|---|
| `runtime/_dl/Node.zip` | 约 111 MB 原始包，装好后可删 |
| `runtime/_dl/vc_redist.x64.exe` | VC++ 安装包，装好后可删 |
| `runtime/_dl/OneKey.zip` 及其解包目录 | 已失效的发行包，仅作参考存档，可删 |

### 10.4 相关记忆文件（如有）

若在同一项目目录下继续工作，以下文件与本篇互为补充（均在 `<项目根>/.workbuddy/memory/` 下）：

| 文件 | 内容 |
|---|---|
| `MEMORY.md` | 项目长期记忆：目标、部署约定、必须记住的坑、用户偏好 |
| `YYYY-MM-DD.md` | 当日工作日志：各阶段的完整过程与实测结论 |

> 在**全新设备**上开工时这些文件不存在，直接以本文档为准即可。

### 10.5 关键外部链接

| 用途 | 地址 |
|---|---|
| NapCat 官方文档 | <https://napneko.github.io/guide/napcat> |
| NapCat Releases | <https://github.com/NapNeko/NapCatQQ/releases> |
| NapCat 完整 API 文档 | <https://napcat.apifox.cn> |
| VC++ 运行库 | <https://aka.ms/vs/17/release/vc_redist.x64.exe> |
| GitHub 镜像加速 | `https://ghfile.geekertao.top/https://github.com/...` |

### 10.6 术语表

| 术语 | 含义 |
|---|---|
| NTQQ | 腾讯新版 QQ 客户端内核 |
| OneBot 11 | 一套 QQ 机器人通信协议规范（注：官方认为该规范已停滞，NapCat 为「尽量遵守 + 差异化扩展」） |
| 正向 WS | WS 服务端模式，业务程序主动连 NapCat |
| 反向 WS | WS 客户端模式，NapCat 主动连业务程序（最常用） |
| PacketBackend | NapCat 的扩展能力模块（DLC），多平台已内置，提供伪造合并转发、Markdown、OCR 等能力 |
| Stream API | v4.8.115+ 引入的大文件流式传输接口，action 名以 `stream` 结尾 |

---

*本文档由实机验证过程整理而成。若与 NapCat 官方文档冲突，以实测结果 + 官方文档交叉验证为准。*
