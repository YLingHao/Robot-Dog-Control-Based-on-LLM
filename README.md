# Robot-Dog-Control-Based-on-LLM
基于大模型指令微调的机器狗运动控制（待完善）

机器狗平台为云深处绝影Lite3，使用方法，使用开发组件mobaxterm通过ssh连接机器狗IP地址，将文件夹上传到机器狗端，输入python dog_llm_exec_server.py执行监听服务。在连接机器狗的主机用cmd命令端运行python llm_forwarder.py --dog-ip 192.168.1.100 --ollama-url http://localhost:11434 --model qwen3:4b

注意主机上要安装ollama并部署大模型，这边以qwen3:4b为例，原理是主机端调用本地ollama的API访问本地大模型输出json命令并转发给机器狗，机器狗接收到json命令后执行相应动作

启动机器狗端监听服务后可在主机启动powershell命令端测试curl http://192.168.1.100:8000/execute `
  -Method POST `
  -Headers @{ "Content-Type" = "application/json" } `
  -Body '{"actions": [{ "code": "0x21010130", "param": 1.3, "semantic": "move_x" },{ "code": "0x21010135", "param": -45, "semantic": "move_yaw" },{ "code": "0x21010130", "param": 1.3, "semantic": "move_x" },{ "code": "0x21010204" },{ "code": "0x2101020D" },{ "code": "0x2101050B" },{ "code": "0x21010507" },{ "code": "0x2101030C" },{ "code": "0x21010130", "param": 1.3, "semantic": "move_x" }]}'

  机器狗会执行连续动作，通过curl "http://192.168.1.100:8000/result?task_id=[输入动作id]"可以查看动作执行详细情况
