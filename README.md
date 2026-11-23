# qq-multi-model：把任意大模型接进 QQ

一个 exe / 一个命令，把**任何 OpenAI 兼容模型**接进 QQ 聊天。不锁定厂商，聊天里随时用 `/model` 切换。

> 这是多模型版，和单模型版 [deepseek-qq](https://github.com/qi-cluadld/deepseek-qq) 是独立仓库。
> 只想用 DeepSeek、想要最简配置，用那个；想让群友自己挑模型、或者接本地 Ollama，用这个。

## 支持的模型

内置了这些服务商的默认地址和模型，填个 Key 就能用：

| 服务商 | 配置名 | 兜底默认模型 | 环境变量 |
| --- | --- | --- | --- |
| DeepSeek | `deepseek` | `deepseek-flash` | `DEEPSEEK_API_KEY` |
| OpenAI | `openai` | 运行时查询 | `OPENAI_API_KEY` |
| Kimi | `moonshot` | `kimi-k3.1-pro` | `MOONSHOT_API_KEY` |
| 智谱 GLM | `zhipu` | `glm-5.3` | `ZHIPU_API_KEY` |
| 通义千问 | `qwen` | `qwen3.7-plus` | `DASHSCOPE_API_KEY` |
| OpenRouter | `openrouter` | 运行时查询 | `OPENROUTER_API_KEY` |
| 硅基流动 | `siliconflow` | 运行时查询 | `SILICONFLOW_API_KEY` |
| Anthropic | `anthropic` | 运行时查询 | `ANTHROPIC_API_KEY` |
| 本地 Ollama | `ollama` | 本机已 pull 的 | 不需要 Key |

> **模型名单不是写死的。** 填完 Key 后，程序会直接问服务商的 `/models` 接口要你账号下真实可用的模型，向导里列出来让你选；聊天里 `/model` 也是实时查询（缓存 10 分钟）。上面那些名字只是连不上服务时的兜底。

**任何其它 OpenAI 兼容服务**（自建 vLLM、One-API、中转站、Groq、Together……）都能接：在 `providers` 里用 `custom_` 前缀加一段 `base_url` + `api_key` 即可，不用改代码。

## 快速开始

### 1. 准备

- 一个用来挂机器人的 QQ 号（**强烈建议小号**，见文末注意事项）
- 至少一家模型服务的 API Key（用 Ollama 的话不用）

### 2. 运行，跟着向导走

把程序放到一个可写目录，双击运行。第一次会进配置向导：

1. **用哪种方式接 QQ** —— 回车就是 NapCat
2. **用哪家的模型** —— 列出上面那张表，选一个填 Key（可以顺手验证）
3. **监听端口 / 访问令牌** —— 默认 6700，令牌回车自动生成
4. **要不要帮你下载 NapCat** —— 默认不下载，同意了才下，下完校验 SHA256

配完自动写好 `config.yaml`，直接启动。想重配：`qq-multi-model --setup`。

### 3. 装 NapCat（如果向导没帮你下）

NapCat 是第三方 QQ 协议端，负责收发消息。到 <https://github.com/NapNeko/NapCatQQ/releases> 下载，登录 QQ 后打开 WebUI（默认 <http://localhost:6099>）→ 网络配置 → 新建「WebSocket 服务器」：

```
地址:      ws://127.0.0.1:6700/onebot
消息格式:  array
Token:     向导里生成的那个（也在 config.yaml 的 onebot.access_token）
```

看到 `QQ 端已连接` 就成功了，私聊它或群里 @ 它。

## 切换模型

聊天里直接发命令，**每个会话独立记着自己的模型**：

| 命令 | 作用 |
| --- | --- |
| `/model` | 看当前模型 + 该服务商账号下的真实模型列表 |
| `/model openai` | 看 OpenAI 账号下有哪些模型 |
| `/model openai/<模型名>` | 切到别家（名字按 `/model` 列出的填） |
| `/model ollama/<模型名>` | 切到本地 Ollama |
| `/model reset` | 回到配置文件里的默认模型 |
| `/reset` | 清空上下文（不影响模型选择） |

切换后上下文保留，可以从上一句接着聊。

## 配置说明

```yaml
defaults:
  model: "deepseek/deepseek-chat"   # provider/模型
  temperature: 1.0
  max_tokens: 2048
  timeout: 60
  retries: 2
  system_prompt: |
    你是一个通过 QQ 聊天的 AI 助手……

providers:
  deepseek:
    api_key: "sk-..."               # 留空则读 DEEPSEEK_API_KEY
  ollama:
    base_url: "http://127.0.0.1:11434/v1"
  custom_myapi:
    base_url: "https://example.com/v1"
    api_key: "..."
```

- 密钥读取顺序：`providers.<名字>.api_key` → 环境变量 → 报错
- `base_url` 同理：配置里的优先，否则用内置默认
- 对话行为（上下文轮数、冷却、并发、分条长度）在 `chat` 段
- QQ 相关配置在 `onebot` / `qqofficial` 段

## 自己接入一家新服务

不用改代码，加一个 `custom_` 前缀的配置就行：

```yaml
providers:
  custom_groq:
    base_url: "https://api.groq.com/openai/v1"
    api_key: "gsk_..."

defaults:
  model: "custom_groq/llama-3.3-70b-versatile"
```

只要对方兼容 `POST {base_url}/chat/completions`，就能用。

## macOS

下载 `qq-multi-model-macos-arm64.zip`（Apple 芯片），解压后首次运行前在终端执行一次：

```bash
xattr -dr com.apple.quarantine . && chmod +x start-mac.command qq-multi-model
```

然后双击 `start-mac.command`。或者自己构建：`bash build_mac.sh`（需要 Python 3.10+）。

## 常见问题

**`/model` 列出的模型和我想的不一样**
内置列表只是「推荐」，直接 `/model 服务商/任意模型名` 就行，不校验存在性——打错了会在调用时返回 404。

**Ollama 连不上**
先在终端跑 `ollama serve`，并确认 `ollama ls` 里有你要的模型；`base_url` 默认 `http://127.0.0.1:11434/v1`。

**切换模型后报错「没有配置 Key」**
那家的 Key 没填。要么 `/model reset` 切回去，要么重新跑 `--setup` 补上。

**群里不回复**
默认必须 @ 才回，也可以用 `#` 开头触发；检查 `onebot.group_need_at` 和 `group_prefix`。

**端口被占用**
改 `onebot.port`，同时改 NapCat 里的反向 WS 地址。

## 开发与测试

```
app/
├─ wizard.py       首次运行向导
├─ providers.py    服务商注册表（内置地址 / 默认模型 / 环境变量名）
├─ llm.py          统一调用层（OpenAI 兼容 + Anthropic 原生，重试与错误翻译）
├─ napcat.py       NapCat 探测 / 下载 / 校验 / 安全解压
├─ chat.py         会话、限流、命令（/model /reset）
├─ onebot.py       OneBot v11 反向 WS
├─ qqofficial.py   QQ 官方机器人
├─ net.py          certifi TLS
└─ config.py       配置加载与校验

tests/
├─ test_llm.py             模型层（假 OpenAI / 假 Anthropic 服务）
├─ test_wizard.py          向导脚本化输入
├─ test_napcat.py          下载器
├─ test_wizard_napcat.py   向导 + 下载集成
└─ e2e.py                  机器人端到端
```

```bash
python tests\test_llm.py
python tests\e2e.py dist\qq-multi-model.exe
```

打包：`build.bat`（Windows）/ `build_mac.sh`（macOS）。

## 注意事项

- **封号风险**：NapCat 是非官方协议端，违反 QQ 用户协议，**务必用小号**。
- **别暴露到公网**：`onebot.host` 保持 `127.0.0.1`，并设一个随机的 `access_token`。
- **费用**：各家按 token 计费，程序里有冷却和并发上限，但群大的话留意用量。
- 只做文本对话，不支持图片、语音、文件。

## 许可证

[MIT](LICENSE) © 2026 qi-cluadld
