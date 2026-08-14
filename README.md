# Robot-Dog-Control-Based-on-LLM

基于大语言模型（LLM）指令控制的机器狗运动控制系统。主机端通过本地大模型将自然语言/语音指令解析为**中间语义 JSON**，经 HTTP 转发给机器狗端监听服务，由机器狗端解析并执行对应动作（移动、步态切换、姿态调整、特技、急停等）。

LLM-based motion control system for quadruped robots. Natural language / voice commands are parsed into **intermediate semantic JSON** by a local LLM running on the host, forwarded to the robot-side listening service over HTTP, and then executed as actual motions (locomotion, gait switching, posture adjustment, tricks, emergency stop, etc.).

---

## 中文简介

本项目面向**云深处绝影 Lite3** 四足机器狗，实现"自然语言 → 机器人动作"的端到端控制链路：

1. **主机端**（`host side/`）：调用本地部署的 Ollama 大模型（微调后的 Qwen3-4B），将用户的中文命令转换为中间语义 JSON（`action_count` + `action_sequence`），支持流式输出、think 内容过滤、JSON 纠错修复与动作知识库（RAG）提示增强；同时负责通过 SSH 自动启动/停止机器狗端监听服务。提供命令行（`llm_forwarder.py`）与图形界面（`llm_forwarder_gui.py`，支持语音输入）两种交互方式。
2. **机器狗端**（`dog_llm_exec/`）：零第三方依赖的常驻 HTTP 服务（`dog_llm_exec_server.py`），接收中间语义 JSON 后由 `dog_llm_exec.py` 解析映射为协议动作码并执行，支持动作队列、任务状态查询、软急停、动作完成后恢复默认步态/姿态，以及可选避障功能。

**本地大语言模型已开源**：本项目使用的微调模型（Qwen3-4B，GGUF 格式）可在 ModelScope 免费下载：
https://www.modelscope.cn/models/YLingHao/Qwen3-4B-dog-control-new-gguf

模型文件名：`qwen3-4b-instruct-new-q4km.gguf`（Q4_K_M 量化，约 2.5 GB），Ollama 中的模型名默认为 `qwen3-4b-instruct-new`。

---

## English Introduction

This project targets the **Unitree-style Jueying Lite3 (DeepRobotics 绝影 Lite3)** quadruped robot and implements an end-to-end "natural language → robot motion" control pipeline:

1. **Host side** (`host side/`): calls a locally deployed Ollama LLM (a fine-tuned Qwen3-4B) to convert user commands in Chinese into intermediate semantic JSON (`action_count` + `action_sequence`). It supports streaming output, `think` content filtering, JSON repair, and an action-knowledge-base (RAG) prompt augmentation. It also manages the robot-side listener service over SSH (auto start/stop). Both a CLI (`llm_forwarder.py`) and a GUI (`llm_forwarder_gui.py`, with voice input) are provided.
2. **Robot side** (`dog_llm_exec/`): a zero-dependency HTTP service (`dog_llm_exec_server.py`) that receives the intermediate semantic JSON. `dog_llm_exec.py` maps it to protocol action codes and executes the motions, with action queueing, task status query, soft emergency stop, automatic restoration of default gait/posture after a task, and optional obstacle avoidance.

**The fine-tuned local LLM is open-sourced**: the model used by this project (Qwen3-4B, GGUF) is freely available on ModelScope:
https://www.modelscope.cn/models/YLingHao/Qwen3-4B-dog-control-new-gguf

Model file: `qwen3-4b-instruct-new-q4km.gguf` (Q4_K_M quantized, ~2.5 GB). The default Ollama model name is `qwen3-4b-instruct-new`.

---

## 系统架构 / Architecture

```
┌────────────────────────── 主机端 Host ──────────────────────────┐
│  用户输入（文本 / 语音 GUI）                                       │
│        │                                                         │
│        ▼                                                         │
│  llm_forwarder.py / llm_forwarder_gui.py                         │
│    ├─ 动作知识库 action_kb.py + action_kb_data.json（RAG 提示增强） │
│    ├─ 调用 Ollama（本地大模型 qwen3-4b-instruct-new）              │
│    ├─ 过滤 think → 提取 / 纠错 / 校验 JSON（JSONPipeline）          │
│    └─ HTTP POST /execute 转发中间语义 JSON                         │
│        │                                                         │
│        │ SSH（自动启动/停止监听服务）                               │
└────────┼────────────────────────────────────────────────────────┘
         ▼
┌────────────────────────── 机器狗端 Robot ────────────────────────┐
│  dog_llm_exec_server.py（HTTP 监听，端口 8000）                    │
│    ├─ POST /execute          提交动作序列 → task_id                │
│    ├─ GET  /result?task_id=  查询任务状态/结果                      │
│    ├─ POST /emergency_stop   软急停（抢占，取消队列）                │
│    └─ GET  /health           健康检查                               │
│  dog_llm_exec.py（中间语义 → 协议动作码 → 执行，UDP 43893）           │
└──────────────────────────────────────────────────────────────────┘
```

---

## 快速开始 / Quick Start

### 环境要求 / Requirements

- 机器狗：云深处绝影 Lite3（已联网，可 SSH 登录，默认账号 `root`）
- 主机：Windows / Linux / macOS，安装 Python 3.8+
- 主机需安装 [Ollama](https://ollama.com) 并部署微调模型

### 第 1 步：下载并部署本地大模型（主机端）

1. 从 ModelScope 下载 GGUF 模型：
   https://www.modelscope.cn/models/YLingHao/Qwen3-4B-dog-control-new-gguf
   ```bash
   # 方式一：git clone
   git clone https://www.modelscope.cn/YLingHao/Qwen3-4B-dog-control-new-gguf.git
   # 方式二：modelscope SDK
   pip install modelscope
   python -c "from modelscope import snapshot_download; snapshot_download('YLingHao/Qwen3-4B-dog-control-new-gguf')"
   ```
2. 导入 Ollama：
   ```bash
   # 编写 Modelfile
   echo "FROM ./qwen3-4b-instruct-new-q4km.gguf" > Modelfile
   ollama create qwen3-4b-instruct-new -f Modelfile
   ollama run qwen3-4b-instruct-new   # 验证可用
   ```

> 也可以直接用 Ollama 支持的其他模型测试（如 `qwen3:4b`），在启动命令中通过 `--model` 指定即可。

### 第 2 步：部署机器狗端监听服务

使用 Mobaxterm / Xshell 等工具通过 SSH 连接机器狗（默认 `root@<机器狗IP>`），将 `dog_llm_exec` 文件夹上传到机器狗（例如 `/root/opt/dog_llm_exec/`），然后执行：

```bash
cd /root/opt/dog_llm_exec/
python dog_llm_exec_server.py
```

服务监听 `0.0.0.0:8000`，无第三方依赖，可在无互联网环境运行。

> 主机端的 `llm_forwarder.py` / GUI 会自动通过 SSH 启动/停止该服务，无需手动登录机器狗。

### 第 3 步：启动主机端转发程序

#### 命令行方式（CLI）

```bash
cd "host side"
pip install requests paramiko        # 依赖

python llm_forwarder.py \
  --dog-ip 192.168.1.100 \
  --ollama-url http://localhost:11434 \
  --model qwen3-4b-instruct-new
```

启动后在 `>` 提示符中输入自然语言命令，例如：

```
> 前进1米然后右转45度
> 站起来，打个招呼
> 切换高速步态
> 急停
```

输入 `exit` / `quit` 退出，程序会自动停止机器狗端监听服务。

#### 图形界面方式（GUI，支持语音）

```bash
cd "host side"
python llm_forwarder_gui.py
```

或直接双击 `双击启动图形界面.bat`（需按文档配置 Python 路径）。界面支持：

- 填写机器狗 IP、SSH 密码、Ollama 地址、模型名，一键「启动」
- 文本 / 语音（Whisper）输入，流式显示模型输出、think 内容与最终 JSON
- 一键「终止」停止狗端监听服务

语音功能依赖安装说明见 [语音依赖安装及图形界面启动说明.md](dog_llm_exec/语音依赖安装及图形界面启动说明.md)。

---

## HTTP API 说明 / API Reference

机器狗端服务（默认端口 8000）：

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/execute` | POST | 提交中间语义 JSON，返回 `{ok, task_id}` |
| `/result?task_id=xxx` | GET | 查询任务状态与动作执行详情 |
| `/emergency_stop` | POST | 软急停（最高优先级，取消队列中未开始任务） |
| `/health` | GET | 健康检查 |

手动测试（PowerShell）：

```powershell
curl http://192.168.1.100:8000/execute `
  -Method POST `
  -Headers @{ "Content-Type" = "application/json" } `
  -Body '{"action_count":1,"action_sequence":[{"step_id":1,"action_type":"locomotion","action_name":"move","direction":"forward","target":{"distance_m":1.0}}]}'
```

查询执行结果：

```powershell
(Invoke-WebRequest "http://192.168.1.100:8000/result?task_id=[任务ID]").Content
```

---

## 中间语义 JSON 格式 / Intermediate Semantic JSON

主机端大模型输出（经提取/纠错/校验后）的中间语义 JSON 格式：

```json
{
  "action_count": 2,
  "action_sequence": [
    {
      "step_id": 1,
      "action_type": "locomotion",
      "action_name": "move",
      "direction": "forward",
      "target": { "distance_m": 1.0 }
    },
    {
      "step_id": 2,
      "action_type": "locomotion",
      "action_name": "turn",
      "direction": "right",
      "target": { "angle_deg": 45.0 }
    }
  ]
}
```

- `action_type` 可选：`locomotion`（移动）、`gait_switch`（步态切换）、`posture_adjust`（姿态调整）、`trick`（特技）、`state_control`（状态控制）、`safety`（安全/急停）
- `action_name` 与 `target` 的完整映射见 [动作及指令一览.md](动作及指令一览.md)
- 主机端动作知识库（`host side/action_kb_data.json`）中定义了每个动作的别名与参数约束，用于提示增强与 JSON 校验

---

## 项目结构 / Repository Layout

```
├── host side/                          # 主机端
│   ├── llm_forwarder.py                # 命令行转发程序（Ollama → JSON → 机器狗）
│   ├── llm_forwarder_gui.py            # 图形界面（支持语音输入）
│   ├── action_kb.py                    # 动作知识库检索（RAG 提示增强）
│   ├── action_kb_data.json             # 动作知识库数据（别名/参数/前置状态）
│   └── 双击启动图形界面.bat            # GUI 一键启动脚本
├── dog_llm_exec/                       # 机器狗端
│   ├── dog_llm_exec_server.py          # 常驻 HTTP 服务（零依赖）
│   ├── dog_llm_exec.py                 # 动作执行器（中间语义 → 协议码）
│   ├── obstacle_avoidance.py           # 避障管理（可选）
│   ├── avoidance_models/               # 避障模型文件
│   ├── command/ sendcommand/ speeds/ socketnetwork/ ...  # 底层控制模块
│   ├── 微调指南.md                      # 大模型微调与部署指南（LLaMA-Factory）
│   └── 语音依赖安装及图形界面启动说明.md  # 语音/GUI 依赖安装说明
├── 动作及指令一览.md                    # 动作序列码与 JSON 指令文档
├── 避障功能说明.md                      # 避障功能集成说明
└── README.md
```

---

## 相关文档 / Documentation

| 文档 | 说明 |
| --- | --- |
| [动作及指令一览.md](动作及指令一览.md) | 所有动作的序列码、参数范围、执行前提与 JSON 指令格式 |
| [避障功能说明.md](避障功能说明.md) | 避障功能（雷达 + 图像识别）集成说明 |
| [微调指南.md](dog_llm_exec/微调指南.md) | 基于 LLaMA-Factory + LoRA 的模型微调、论文图表生成与发布指南 |
| [语音依赖安装及图形界面启动说明.md](dog_llm_exec/语音依赖安装及图形界面启动说明.md) | FFmpeg / Whisper 等语音依赖与 GUI 启动脚本配置 |

---

## 模型 / Model

- 基座模型：Qwen3-4B（Instruct）
- 微调方式：LoRA 指令微调（见 [微调指南.md](dog_llm_exec/微调指南.md)）
- 开源地址（GGUF）：https://www.modelscope.cn/models/YLingHao/Qwen3-4B-dog-control-new-gguf
- Ollama 模型名：`qwen3-4b-instruct-new`

---

## License

Apache License 2.0
