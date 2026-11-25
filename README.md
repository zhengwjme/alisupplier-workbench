# AliSupplier 客户跟进工作台

这个工具读取阿里卖家客户端日志和能确认归属的卡片数据，将采集结果存入本机 SQLite。浏览器首页展示每位客户的最新变化、当前概要、建议跟进和聊天依据。

## 打开客户端

1. 在 `dist/` 选择与你的电脑匹配的 ZIP。Apple 芯片 Mac 选 `darwin-arm64`，Intel Mac 选 `darwin-x64`，Windows x64 选 `win32-x64`。
2. 解压后打开 `AliSupplier-Workbench.app`（Mac）或 `AliSupplier-Workbench.exe`（Windows）。Windows 请保留解压后同目录的文件。
3. 点击「启动服务」。工作台会在应用窗口内打开，原来的 SQLite、概要和 AI 设置会继续使用。
4. 第一次使用时，在「AI 与设置」中添加并选择文字模型。测试成功后，收到客户新消息便会按设置汇总。

工作台顶部「服务控制」可停止服务。关闭客户端窗口会停止后台采集和汇总，重开后待处理批次会从 SQLite 继续。发行包自带 Python，使用看图模型无需另装 OCR。想启用本地 OCR 时，仍需 Tesseract。

发行包用于本地使用。Mac 版本使用临时签名，未做 Apple 公证。Windows 版本未附 Authenticode 签名。

## 汇总规则

- 默认由客户新消息触发。按账号和会话合并消息，静默 3 分钟或从本批第一条起等满 10 分钟，以先到为准。
- 10 分钟是提交后台的等待上限。模型处理和积压任务会增加页面出结果的时间。
- 没有客户新消息时，打开页面、我方单独回复、重复扫描和附件补齐均不会启动新一轮自动汇总。
- 卡片直接读取已采到的字段。附件识别有单独的时间预算，文字概要可先发布。晚到的内容随下一批处理，也可以手动汇总。
- 现有数据库可直接使用。首次扫描旧日志时，超过 15 分钟的历史消息默认作为背景保留。现有概要只覆盖能采到的记录。

## AI、图片与用量

- 支持 OpenAI Chat Completions 兼容、OpenAI Responses、Anthropic Messages 和 Gemini。地址通常填写到版本前缀，例如 `https://api.openai.com/v1`。
- 可以保存多套配置，分别选文字和看图模型。外部接口需要 HTTPS。本机接口可以使用 HTTP。
- 图片发送给外部模型需要在设置中开启。可选本地 OCR 依赖 Tesseract，程序从 PATH 查找，中文识别需要相应语言包。
- 归属不明、文件丢失或识别冲突的图片保留状态。可以打开原图，补充或纠正图片说明，再手动汇总。
- 每日预算按本机日期计算。无法取得模型实报用量时，以请求预估用量记录。预算填 0 表示不限。
- 已配置的外部文字模型会收到汇总所需的聊天内容及已启用的图片识别结果。密钥和接口配置保存在数据库旁的 `messages.db.ai-settings.json`，当作敏感文件保管。

## 本地数据和排查

| 项目 | 路径或入口 |
|---|---|
| macOS 日志 | `~/Library/Application Support/AliSupplier/app.log` |
| Windows 日志 | `%APPDATA%\AliSupplier\app.log` |
| macOS 数据库 | `~/Library/Application Support/AliSupplierHook/messages.db` |
| Windows 数据库 | `%LOCALAPPDATA%\AliSupplierHook\messages.db` |
| 自定义路径 | 启动参数 `--log`、`--db`、`--port`、`--no-browser`。也可用 `HOOK_LOG`、`HOOK_DB`、`HOOK_PORT`。 |
| 采集排查 | 新工作台侧栏「采集调试」，或 `/logs`。 |

升级前关闭服务并备份数据库。图片原件可能仍在客户端缓存，缓存清理后，消息记录和文字概要仍可查看。卡片库目录的账号归属无法可靠识别时会跳过，终端会提示。日志里未记录的历史消息、引用或已清理附件，无法从工作台补回。


## 开发与打包

Node.js 22.12+。进入 `desktop/` 后执行 `npm ci`。开发模式 `npm start` 使用本机 Python 3，默认沿用已有的客户数据库。

```bash
npm run build:desktop -- --platform=darwin --arch=arm64
npm run build:desktop -- --platform=darwin --arch=x64
npm run build:desktop -- --platform=win32 --arch=x64
```

Mac 发行包需在 macOS 构建。Windows ZIP 可以在 macOS 或 Windows 构建。打包脚本对 Electron、Python 和证书数据验证 SHA-256。输出 ZIP 和校验值位于 `dist/`。桌面后端使用动态本机端口，Electron 自动转发认证信息。新旧入口需使用同一份 SQLite 时，请先停止正在运行的旧服务。

开发或排查时仍可单独运行原 Python 服务。若要隔离日志、卡片和图片来源，可用 `HOOK_APP_SUPPORT_DIR` 指定临时应用数据目录，并用 `HOOK_DB` 指定测试库。

Windows x64 的原生构建在 GitHub Actions 的 **Build Windows** 中运行。构建成功后，从该次运行的 Artifacts 下载 ZIP 和 SHA-256 校验文件。
