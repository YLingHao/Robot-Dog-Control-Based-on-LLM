# Robot-Dog-Control-Based-on-LLM
基于大模型指令微调的机器狗运动控制（待完善）

机器狗平台为云深处绝影Lite3，使用方法，使用开发组件mobaxterm通过ssh连接机器狗IP地址，将文件夹上传到机器狗端，输入python dog_llm_exec_server.py执行监听服务。在连接机器狗的主机用cmd命令端运行python llm_forwarder.py --dog-ip 192.168.1.100 --ollama-url http://localhost:11434 --model qwen3:4b

注意主机上要安装ollama并部署大模型，这边以qwen3:4b为例，原理是主机端调用本地ollama的API访问本地大模型输出json命令并转发给机器狗，机器狗接收到json命令后执行相应动作
