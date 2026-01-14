# 大模型输出转发程序使用说明

## 功能说明

`llm_forwarder.py` 是一个自动转发程序，用于监听本地ollama大模型的输出，并将符合要求的JSON格式指令自动转发到机器狗执行。

## 主要特性

1. **自动监听**：监听ollama大模型的输出（stdin或API响应）
2. **智能提取**：自动识别并提取JSON格式的指令
3. **自动转发**：将指令转发到机器狗执行
4. **自动管理**：自动启动/停止机器狗上的监听程序

## 安装依赖

```bash
pip install requests
```

## 使用方法

### ⚠️ 重要说明

**当前程序监听的是标准输入（stdin）**，这意味着：

- ✅ **可以通过管道使用**：`ollama run model "command" | python llm_forwarder.py ...`
- ✅ **可以手动输入**：运行程序后，直接粘贴JSON指令
- ❌ **不能直接监听ollama Web界面**：Web界面的输出不会自动被捕获
- ❌ **不能直接监听ollama命令行**：需要通过管道连接

### 1. 通过管道使用（推荐）

**方式A：ollama命令行 + 管道**

```bash
# 启动转发程序（保持运行）
python llm_forwarder.py --dog-ip 192.168.1.100 --dog-user root

# 在另一个终端中运行ollama，输出会自动转发
ollama run llama2 "让机器狗前进0.5米然后右转45度" | python llm_forwarder.py --dog-ip 192.168.1.100
```

**方式B：ollama API + 管道**

```bash
# 使用ollama API，将输出通过管道传递
curl http://localhost:11434/api/generate -d '{
  "model": "llama2",
  "prompt": "让机器狗前进0.5米"
}' | python llm_forwarder.py --dog-ip 192.168.1.100
```

### 2. 监听文件变化

如果ollama输出到文件，可以监听文件变化：

```bash
# 设置ollama输出到文件（需要配置ollama）
# 然后监听文件
python llm_forwarder.py --dog-ip 192.168.1.100 --watch-file /path/to/ollama_output.txt
```

### 3. 手动输入模式

启动程序后，直接粘贴JSON指令：

```bash
python llm_forwarder.py --dog-ip 192.168.1.100

# 程序启动后，直接粘贴JSON：
{"actions":[{"code":"0x21010130","param":0.5,"semantic":"move_x"}]}
```

### 4. 在ollama Web界面中使用（需要配合）

**方法1：复制粘贴**
1. 启动转发程序（保持运行）
2. 在ollama Web界面中与大模型交互
3. 当大模型输出JSON指令时，复制JSON
4. 粘贴到转发程序的终端窗口

**方法2：使用ollama CLI包装脚本**

创建一个脚本 `ollama_with_forwarder.sh`：

```bash
#!/bin/bash
# 启动转发程序（后台运行）
python llm_forwarder.py --dog-ip 192.168.1.100 &
FORWARDER_PID=$!

# 运行ollama，输出通过管道传递给转发程序
ollama run "$@" | python llm_forwarder.py --dog-ip 192.168.1.100

# 清理
kill $FORWARDER_PID 2>/dev/null
```

### 5. 停止程序

按 `Ctrl+C` 停止程序，程序会自动：
- 停止机器狗上的监听程序
- 清理资源并退出

## 命令行参数

- `--dog-ip` (必需): 机器狗IP地址，例如 `192.168.1.100`
- `--dog-user` (可选): SSH用户名，默认 `root`
- `--dog-port` (可选): 机器狗HTTP服务端口，默认 `8000`
- `--ssh-port` (可选): SSH端口，默认 `22`
- `--text` (可选): 直接转发文本中的JSON指令（用于测试）

## JSON指令格式

大模型输出的JSON指令必须符合以下格式：

```json
{
  "actions": [
    {
      "code": "0x21010130",
      "param": 0.5,
      "semantic": "move_x"
    },
    {
      "code": "0x21010204"
    }
  ]
}
```

### 支持的提取方式

程序支持多种JSON提取方式：

1. **纯JSON格式**：
   ```json
   {"actions": [...]}
   ```

2. **JSON代码块**：
   ```json
   ```json
   {"actions": [...]}
   ```
   ```

3. **Markdown代码块**：
   ```
   ```
   {"actions": [...]}
   ```
   ```

4. **文本中的JSON**：自动识别文本中的第一个完整的JSON对象

## 示例

### 示例1：基本转发

```bash
# 终端1：启动转发程序
python llm_forwarder.py --dog-ip 192.168.1.100

# 终端2：与大模型交互
ollama run llama2 "让机器狗前进0.5米然后右转45度"
# 大模型输出JSON时，会自动转发到机器狗
```

### 示例2：测试模式

```bash
python llm_forwarder.py --dog-ip 192.168.1.100 --text '{"actions":[{"code":"0x21010130","param":0.5,"semantic":"move_x"}]}'
```

## 注意事项

1. **SSH连接**：确保电脑可以SSH连接到机器狗（可能需要配置SSH密钥）
2. **网络连接**：确保电脑可以访问机器狗的HTTP服务（默认8000端口）
3. **权限**：确保SSH用户有权限执行命令和访问 `/root/opt/dog_llm_exec` 目录
4. **Windows系统**：如果SSH命令不可用，可以安装OpenSSH或PuTTY

## 故障排除

### 问题1：SSH连接失败

**错误信息**：`SSH命令执行失败: ...`

**解决方案**：
- 检查SSH连接：`ssh root@192.168.1.100`
- 检查SSH密钥配置
- Windows系统可能需要安装OpenSSH客户端

### 问题2：无法启动监听程序

**错误信息**：`启动监听程序失败`

**解决方案**：
- 检查机器狗上的路径：`/root/opt/dog_llm_exec/dog_llm_exec_server.py`
- 检查Python3是否可用：`ssh root@192.168.1.100 "python3 --version"`
- 手动检查服务：`ssh root@192.168.1.100 "ps aux | grep dog_llm_exec_server"`

### 问题3：JSON指令未被识别

**错误信息**：没有输出或没有转发

**解决方案**：
- 检查JSON格式是否正确
- 确保JSON中包含 `actions` 字段
- 使用 `--text` 参数测试JSON提取功能

### 问题4：指令发送失败

**错误信息**：`指令发送失败: ...`

**解决方案**：
- 检查机器狗HTTP服务是否正常运行：`curl http://192.168.1.100:8000/health`
- 检查网络连接
- 查看机器狗上的服务日志

## 技术细节

- **SSH执行**：使用subprocess执行SSH命令
- **HTTP请求**：使用requests库发送HTTP POST请求
- **JSON提取**：使用正则表达式和JSON解析器提取JSON
- **信号处理**：捕获SIGINT和SIGTERM信号，确保优雅退出
