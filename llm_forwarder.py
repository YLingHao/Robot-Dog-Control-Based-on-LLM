#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""大模型输出转发程序

功能：
1. 提供交互式命令行界面，用户可以直接输入请求
2. 调用ollama本地API获取大模型响应
3. 自动识别并提取JSON格式的指令（过滤think部分）
4. 转发到机器狗执行
5. 自动管理机器狗上的监听程序（启动/停止）

使用方法：
1. 启动转发程序：python llm_forwarder.py --dog-ip 192.168.1.100 --ollama-url http://localhost:11434 --model qwen3:4b
2. 在命令行界面中输入自然语言请求，程序会自动调用大模型并转发JSON指令
3. 输入 'exit' 或 'quit' 退出程序，会自动清理机器狗上的监听程序
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

try:
    import requests
except ImportError:
    print("错误：缺少 requests 库，请运行: pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


class DogController:
    """机器狗控制器：负责SSH连接和HTTP请求"""
    
    def __init__(
        self,
        dog_ip: str,
        dog_user: str = "root",
        http_port: int = 8000,
        udp_port: int = 43893,
        ssh_port: int = 22,
        passwords: Optional[list] = None,
    ):
        self.dog_ip = dog_ip
        self.dog_user = dog_user
        self.http_port = http_port
        self.udp_port = udp_port
        self.ssh_port = ssh_port
        self.base_url = f"http://{dog_ip}:{http_port}"
        self.server_path = "/root/opt/dog_llm_exec"
        self.server_script = "dog_llm_exec_server.py"
        self.passwords = passwords or ["1", "root"]  # 默认尝试的密码列表
        self.python_cmd = None  # 检测到的Python命令，将在start_server时设置
        self._ssh_client = None  # 保持的SSH连接
        self._ssh_password = None  # 成功认证的密码
        
    def _connect_ssh(self) -> bool:
        """建立并保持SSH连接"""
        if self._ssh_client is not None:
            # 检查连接是否仍然有效
            try:
                if self._ssh_client.get_transport() and self._ssh_client.get_transport().is_active():
                    return True
            except:
                pass
            # 连接无效，关闭并重新连接
            try:
                self._ssh_client.close()
            except:
                pass
            self._ssh_client = None
        
        # 尝试使用paramiko建立连接
        try:
            import paramiko
        except ImportError:
            logging.error("未安装paramiko库，无法保持SSH连接，请安装: pip install paramiko")
            return False
        
        # 尝试使用已保存的密码，如果没有则尝试所有密码
        passwords_to_try = [self._ssh_password] if self._ssh_password else self.passwords
        
        for password in passwords_to_try:
            if password is None:
                continue
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    self.dog_ip,
                    port=self.ssh_port,
                    username=self.dog_user,
                    password=password,
                    timeout=5
                )
                self._ssh_client = ssh
                self._ssh_password = password
                logging.info(f"✓ SSH连接已建立: {self.dog_user}@{self.dog_ip}:{self.ssh_port}")
                return True
            except paramiko.AuthenticationException:
                continue
            except Exception as e:
                logging.error(f"SSH连接失败: {e}")
                return False
        
        logging.error("所有密码尝试失败，无法建立SSH连接")
        return False
    
    def _disconnect_ssh(self):
        """关闭SSH连接"""
        if self._ssh_client is not None:
            try:
                self._ssh_client.close()
                logging.debug("SSH连接已关闭")
            except:
                pass
            self._ssh_client = None
    
    def _run_ssh_command(self, command: str, timeout: int = 10, use_persistent: bool = True) -> Tuple[bool, str, str]:
        """执行SSH命令（优先使用已建立的连接）"""
        # 如果使用持久连接且已建立连接，使用现有连接
        if use_persistent and self._ssh_client is not None:
            try:
                if self._ssh_client.get_transport() and self._ssh_client.get_transport().is_active():
                    return self._run_ssh_with_existing_connection(command, timeout)
            except:
                # 连接已失效，重置
                self._ssh_client = None
        
        # 如果没有持久连接，使用原来的方式（每次新建连接）
        # 首先尝试使用paramiko（如果已安装）
        try:
            import paramiko
            return self._run_ssh_with_paramiko(command, timeout)
        except ImportError:
            pass
        
        # 尝试使用sshpass (Linux) 或 plink (Windows)
        if os.name == 'nt':
            # Windows: 尝试使用plink
            return self._run_ssh_with_plink(command, timeout)
        else:
            # Linux/Mac: 尝试使用sshpass
            return self._run_ssh_with_sshpass(command, timeout)
    
    def _run_ssh_with_existing_connection(self, command: str, timeout: int = 10) -> Tuple[bool, str, str]:
        """使用已建立的SSH连接执行命令"""
        try:
            stdin, stdout, stderr = self._ssh_client.exec_command(command, timeout=timeout)
            exit_status = stdout.channel.recv_exit_status()
            output = stdout.read().decode('utf-8', errors='ignore')
            error = stderr.read().decode('utf-8', errors='ignore')
            
            if exit_status == 0:
                return True, output, error
            else:
                return False, output, error
        except Exception as e:
            logging.error(f"使用SSH连接执行命令失败: {e}")
            # 连接可能已断开，重置
            self._ssh_client = None
            return False, "", f"SSH命令执行失败: {e}"
    
    def _run_ssh_with_paramiko(self, command: str, timeout: int = 10) -> Tuple[bool, str, str]:
        """使用paramiko执行SSH命令（支持密码认证）"""
        import paramiko
        
        for password in self.passwords:
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(
                    self.dog_ip,
                    port=self.ssh_port,
                    username=self.dog_user,
                    password=password,
                    timeout=5
                )
                
                stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
                exit_status = stdout.channel.recv_exit_status()
                output = stdout.read().decode('utf-8', errors='ignore')
                error = stderr.read().decode('utf-8', errors='ignore')
                
                ssh.close()
                
                if exit_status == 0:
                    return True, output, error
                else:
                    return False, output, error
                    
            except paramiko.AuthenticationException:
                continue  # 尝试下一个密码
            except Exception as e:
                return False, "", f"SSH连接失败: {e}"
        
        return False, "", "所有密码尝试失败，请检查密码或使用SSH密钥"
    
    def _run_ssh_with_plink(self, command: str, timeout: int = 10) -> Tuple[bool, str, str]:
        """使用plink执行SSH命令（Windows PuTTY）"""
        for password in self.passwords:
            try:
                plink_cmd = [
                    "plink",
                    "-ssh",
                    "-batch",
                    "-pw", password,
                    "-P", str(self.ssh_port),
                    f"{self.dog_user}@{self.dog_ip}",
                    command
                ]
                
                result = subprocess.run(
                    plink_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=False
                )
                
                if result.returncode == 0:
                    return True, result.stdout, result.stderr
                    
            except FileNotFoundError:
                return False, "", "未找到plink，请安装PuTTY或使用paramiko库"
            except subprocess.TimeoutExpired:
                return False, "", "SSH命令执行超时"
            except Exception:
                continue  # 尝试下一个密码
        
        return False, "", "所有密码尝试失败，请检查密码或使用SSH密钥"
    
    def _run_ssh_with_sshpass(self, command: str, timeout: int = 10) -> Tuple[bool, str, str]:
        """使用sshpass执行SSH命令（Linux/Mac）"""
        for password in self.passwords:
            try:
                sshpass_cmd = [
                    "sshpass",
                    "-p", password,
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=5",
                    "-p", str(self.ssh_port),
                    f"{self.dog_user}@{self.dog_ip}",
                    command
                ]
                
                result = subprocess.run(
                    sshpass_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=False
                )
                
                if result.returncode == 0:
                    return True, result.stdout, result.stderr
                    
            except FileNotFoundError:
                return False, "", "未找到sshpass，请安装: sudo apt-get install sshpass 或使用paramiko库"
            except subprocess.TimeoutExpired:
                return False, "", "SSH命令执行超时"
            except Exception:
                continue  # 尝试下一个密码
        
        return False, "", "所有密码尝试失败，请检查密码或使用SSH密钥"
    
    def _detect_python_command(self) -> str:
        """检测机器狗上可用的Python解释器命令"""
        # 如果还没有SSH连接，先建立连接
        if self._ssh_client is None:
            if not self._connect_ssh():
                logging.warning("无法建立SSH连接，使用默认Python命令: python3")
                return "python3"
        
        # 按优先级尝试不同的Python命令
        python_commands = ["python3", "python", "python3.9", "python3.8", "python3.7"]
        
        logging.info("正在检测机器狗上的Python解释器...")
        for cmd in python_commands:
            # 检查命令是否存在且可用
            check_cmd = f"which {cmd} && {cmd} --version"
            success, stdout, stderr = self._run_ssh_command(check_cmd, timeout=5, use_persistent=True)
            if success and stdout.strip():
                logging.info(f"✓ 检测到Python解释器: {cmd}")
                return cmd
        
        # 如果都检测不到，默认返回python3（大多数Linux系统都有）
        logging.warning("无法检测Python解释器，将使用默认值: python3")
        return "python3"
    
    def start_server(self) -> bool:
        """启动机器狗上的监听程序"""
        logging.info(f"正在连接机器狗 {self.dog_ip} 并启动监听程序...")
        
        # 0. 建立SSH连接（只连接一次）
        if not self._connect_ssh():
            logging.error("无法建立SSH连接")
            return False
        
        try:
            # 1. 检测Python解释器（如果还未检测）
            if self.python_cmd is None:
                self.python_cmd = self._detect_python_command()
            python_cmd = self.python_cmd
            
            # 2. 先杀死可能存在的旧进程（防止之前没完全杀死）
            logging.info("检查并停止旧的监听程序...")
            kill_cmd = f'pkill -f "{python_cmd}.*dog_llm_exec_server"'
            self._run_ssh_command(kill_cmd, timeout=5, use_persistent=True)
            time.sleep(0.5)
            
            # 再次确认杀死（使用kill -9强制杀死）
            kill_force_cmd = f'ps aux | grep "{python_cmd}.*dog_llm_exec_server" | grep -v grep | awk \'{{print $2}}\' | xargs -r kill -9'
            self._run_ssh_command(kill_force_cmd, timeout=5, use_persistent=True)
            time.sleep(0.5)
            
            # 3. 等待进程完全退出
            time.sleep(1.0)
            
            # 4. 启动新进程（在一个SSH会话中：先cd，再执行python）
            log_path = "/tmp/dog_llm_exec_server.log"
            script_full_path = f"{self.server_path}/{self.server_script}"
            
            # 在一个SSH命令中完成：先cd到目录，再执行python启动服务
            # 使用nohup确保进程在SSH断开后继续运行
            logging.info(f"正在启动监听服务: cd {self.server_path} && {python_cmd} {self.server_script}")
            # 注意：nohup命令在后台执行，SSH返回值可能不可靠，所以不依赖返回值判断成功
            # 而是通过后续的进程检查和健康检查来判断
            # 使用绝对路径的日志文件，避免cd失效的问题
            start_cmd = f"cd {self.server_path} && nohup {python_cmd} {self.server_script} > {log_path} 2>&1 &"
            success, stdout, stderr = self._run_ssh_command(start_cmd, timeout=15, use_persistent=True)
            
            # 不立即返回失败，而是继续检查进程和服务状态
            # 因为后台执行可能返回非0，但实际已经启动成功
            if not success:
                logging.warning(f"启动命令SSH执行可能超时，但继续检查服务状态（nohup后台执行可能已成功）")
            
            # 等待一下让进程启动
            time.sleep(2.0)  # 增加等待时间
            
            # 验证进程是否真的启动了
            check_cmd = f"ps aux | grep '{python_cmd}.*{self.server_script}' | grep -v grep"
            check_success, check_output, _ = self._run_ssh_command(check_cmd, timeout=5, use_persistent=True)
        
            process_running = check_success and check_output.strip()
            if not process_running:
                logging.warning("进程检查未找到运行中的进程，尝试读取日志确认...")
                read_log_success, log_content, _ = self._run_ssh_command(f"cat {log_path}", timeout=5, use_persistent=True)
                if read_log_success and log_content:
                    logging.info(f"远程日志内容:\n{log_content}")
                    # 检查日志中是否有错误信息
                    if "Traceback" in log_content or "Error" in log_content or "error" in log_content.lower():
                        logging.error("日志显示服务启动失败，包含错误信息")
                        return False
                    # 如果日志显示服务已启动，继续健康检查
                    if "HTTP服务已启动" in log_content or "已启动" in log_content:
                        logging.info("日志显示服务可能已启动，继续健康检查...")
                        process_running = True  # 标记为可能已启动
                else:
                    logging.warning("日志文件为空或不存在，可能服务未启动")
                    # 如果进程不存在且日志为空，很可能启动失败
                    # 但继续健康检查，因为可能进程刚启动，日志还没写入
            else:
                logging.info(f"✓ 检测到运行中的进程")
            
            # 5. 等待服务启动并验证健康检查
            logging.info("等待服务启动并验证...")
            health_check_passed = False
            for i in range(40):  # 最多等待20秒
                time.sleep(0.5)
                try:
                    response = requests.get(f"{self.base_url}/health", timeout=2)
                    if response.status_code == 200:
                        health_check_passed = True
                        logging.info(f"✓ 机器狗监听程序已启动 (HTTP端口 {self.http_port})")
                        break
                except Exception as e:
                    if i % 10 == 0:  # 每5秒输出一次等待信息
                        logging.debug(f"等待服务启动中... ({i*0.5:.1f}秒)")
            
            if not health_check_passed:
                # 健康检查失败，再次检查进程和日志
                logging.error("健康检查失败，再次检查进程状态...")
                check_cmd = f"ps aux | grep '{python_cmd}.*{self.server_script}' | grep -v grep"
                check_success, check_output, _ = self._run_ssh_command(check_cmd, timeout=5, use_persistent=True)
            if check_success and check_output.strip():
                logging.warning("进程仍在运行，但健康检查失败，可能是服务启动异常")
            else:
                logging.error("进程已停止，服务启动失败")
            
                # 读取最新日志
                read_log_success, log_content, _ = self._run_ssh_command(f"tail -50 {log_path}", timeout=5, use_persistent=True)
                if read_log_success and log_content:
                    logging.error(f"最新日志内容:\n{log_content}")
                
                logging.error(f"监听程序启动失败，请检查机器狗上的日志: {log_path}")
                return False
            
            return True
        finally:
            # 保持SSH连接，不在这里关闭（在stop_server或程序退出时关闭）
            pass
    
    def stop_server(self) -> bool:
        """停止机器狗上的监听程序"""
        logging.info(f"正在停止机器狗 {self.dog_ip} 上的监听程序...")
        
        # 如果还没有SSH连接，先建立连接
        if self._ssh_client is None:
            if not self._connect_ssh():
                logging.warning("无法建立SSH连接，尝试使用临时连接停止服务")
                # 如果无法建立持久连接，使用临时连接
                if self.python_cmd is None:
                    self.python_cmd = self._detect_python_command()
                kill_cmd = f'pkill -f "{self.python_cmd}.*dog_llm_exec_server"'
                self._run_ssh_command(kill_cmd, timeout=5, use_persistent=False)
                return True
        
        try:
            # 如果还未检测Python命令，先检测（可能在start_server之前就调用了stop_server）
            if self.python_cmd is None:
                self.python_cmd = self._detect_python_command()
            
            # 使用正确的pkill命令（使用检测到的Python命令）
            kill_cmd = f'pkill -f "{self.python_cmd}.*dog_llm_exec_server"'
            success, stdout, stderr = self._run_ssh_command(kill_cmd, timeout=5, use_persistent=True)
            
            if success:
                logging.info("✓ 监听程序已停止")
                return True
            else:
                logging.warning("停止监听程序时未找到运行中的进程（可能已经停止）")
                return True
        finally:
            # 关闭SSH连接
            self._disconnect_ssh()
    
    def send_command(self, payload: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """发送指令到机器狗"""
        try:
            response = requests.post(
                f"{self.base_url}/execute",
                json=payload,
                timeout=30
            )
            if response.status_code == 200:
                result = response.json()
                return True, result
            else:
                return False, {"error": f"HTTP {response.status_code}: {response.text}"}
        except requests.exceptions.RequestException as e:
            return False, {"error": f"请求失败: {e}"}


class JSONExtractor:
    """从文本中提取JSON指令"""
    
    @staticmethod
    def filter_think_content(text: str) -> str:
        """过滤掉think部分的内容"""
        # 移除各种think标签及其内容
        patterns = [
            r'<think>.*?</think>',  # <think>...</think>
            r'<think>.*?</think>',  # <think>...</think>
            r'```think.*?```',  # ```think ... ```
            r'<thinking>.*?</thinking>',  # <thinking>...</thinking>
        ]
        
        for pattern in patterns:
            text = re.sub(pattern, '', text, flags=re.DOTALL | re.IGNORECASE)
        
        # 移除包含"think"关键词的段落（如果think内容在特定标记中）
        lines = text.split('\n')
        filtered_lines = []
        in_think_block = False
        
        for line in lines:
            # 检测think块的开始（多种格式）
            if (re.search(r'think\s*[:：]', line, re.IGNORECASE) or 
                'thinking' in line.lower() or
                line.strip().lower().startswith('think:') or
                line.strip().lower().startswith('thinking:')):
                in_think_block = True
                continue
            
            # 检测think块的结束（通常是空行、JSON开始标记或其他特定标记）
            if in_think_block:
                line_lower = line.strip().lower()
                # 如果遇到空行、JSON开始、response/output标记，结束think块
                if (line.strip() == '' or 
                    line_lower.startswith('{') or
                    line_lower.startswith('response') or
                    line_lower.startswith('output') or
                    line_lower.startswith('json') or
                    re.search(r'^\{', line)):
                    in_think_block = False
                    # 如果是JSON开始标记，保留这一行
                    if line_lower.startswith('{') or re.search(r'^\{', line):
                        filtered_lines.append(line)
                else:
                    continue
            else:
                filtered_lines.append(line)
        
        return '\n'.join(filtered_lines)
    
    @staticmethod
    def extract_json(text: str) -> Optional[Dict[str, Any]]:
        """从文本中提取第一个有效的JSON对象（已过滤think部分）"""
        # 先过滤think内容
        text = JSONExtractor.filter_think_content(text)
        
        # 尝试直接解析整个文本
        text = text.strip()
        if not text:
            return None
        
        # 方法1: 尝试直接解析
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "actions" in data:
                return data
        except json.JSONDecodeError:
            pass
        
        # 方法2: 查找JSON代码块（```json ... ``` 或 ``` ... ```）
        json_block_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
        matches = re.findall(json_block_pattern, text, re.DOTALL)
        for match in matches:
            try:
                data = json.loads(match)
                if isinstance(data, dict) and "actions" in data:
                    return data
            except json.JSONDecodeError:
                continue
        
        # 方法3: 查找第一个 { ... } 结构
        brace_start = text.find('{')
        if brace_start == -1:
            return None
        
        # 从第一个 { 开始，尝试找到匹配的 }
        brace_count = 0
        for i in range(brace_start, len(text)):
            if text[i] == '{':
                brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_str = text[brace_start:i+1]
                    try:
                        data = json.loads(json_str)
                        if isinstance(data, dict) and "actions" in data:
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
        
        return None
    
    @staticmethod
    def validate_command(payload: Dict[str, Any]) -> bool:
        """验证指令格式"""
        if not isinstance(payload, dict):
            return False
        
        if "actions" not in payload:
            return False
        
        if not isinstance(payload["actions"], list):
            return False
        
        if len(payload["actions"]) == 0:
            return False
        
        # 验证每个action的格式
        for action in payload["actions"]:
            if not isinstance(action, dict):
                return False
            if "code" not in action:
                return False
        
        return True


class OllamaAPIProxy:
    """Ollama API透明代理：位于WebUI和Ollama之间，同时转发响应给WebUI和监听程序"""
    
    def __init__(self, forwarder: 'LLMForwarder', ollama_url: str, proxy_port: int = 11435):
        self.forwarder = forwarder
        self.ollama_url = ollama_url
        self.proxy_port = proxy_port
        self.server = None
        self.server_thread = None
    
    def start(self):
        """启动代理服务器"""
        handler = self._create_handler()
        self.server = HTTPServer(('0.0.0.0', self.proxy_port), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        logging.info(f"Ollama API代理已启动，端口: {self.proxy_port}")
        logging.info(f"代理地址: http://localhost:{self.proxy_port}")
        logging.info(f"原Ollama地址: {self.ollama_url}")
        logging.info("")
        logging.info("使用说明：")
        logging.info("  1. 在WebUI或命令行中，将Ollama API地址改为代理地址")
        logging.info(f"     原地址: {self.ollama_url}")
        logging.info(f"     代理地址: http://localhost:{self.proxy_port}")
        logging.info("  2. 正常使用WebUI或命令行与ollama对话")
        logging.info("  3. 程序会自动监听响应，提取JSON指令并转发到机器狗")
        logging.info("  4. 不影响用户正常使用大模型")
    
    def stop(self):
        """停止代理服务器"""
        if self.server:
            self.server.shutdown()
            logging.info("Ollama API代理已停止")
    
    def _create_handler(self):
        forwarder = self.forwarder
        ollama_url = self.ollama_url
        
        class ProxyHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                """代理POST请求：转发到Ollama，同时将响应转发给WebUI和监听程序"""
                try:
                    # 读取请求体
                    content_length = int(self.headers.get('Content-Length', 0))
                    body = self.rfile.read(content_length)
                    
                    # 转发请求到ollama
                    ollama_endpoint = f"{ollama_url}{self.path}"
                    response = requests.post(
                        ollama_endpoint,
                        data=body,
                        headers=dict(self.headers),
                        stream=True,
                        timeout=300
                    )
                    
                    # 处理流式响应（Ollama使用SSE格式：Server-Sent Events）
                    accumulated_text = ""  # 累积的文本内容（用于提取JSON）
                    json_sent = False  # 标记是否已经发送过JSON指令（避免重复发送）
                    
                    # 设置响应头
                    self.send_response(response.status_code)
                    for header, value in response.headers.items():
                        if header.lower() not in ['content-encoding', 'transfer-encoding', 'content-length', 'connection']:
                            self.send_header(header, value)
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()
                    
                    # 读取流式响应并同时转发给WebUI和监听程序
                    # Ollama的SSE格式：每行以 "data: " 开头，然后是JSON数据
                    for line in response.iter_lines():
                        if line:
                            line_str = line.decode('utf-8', errors='ignore')
                            
                            # 解析SSE格式：提取 "data: " 后面的内容
                            if line_str.startswith("data: "):
                                data_content = line_str[6:].strip()  # 去掉 "data: " 前缀
                                if data_content:
                                    try:
                                        # 尝试解析JSON数据
                                        data_json = json.loads(data_content)
                                        # 提取 "response" 字段中的文本内容
                                        if isinstance(data_json, dict) and "response" in data_json:
                                            text_chunk = data_json["response"]
                                            accumulated_text += text_chunk
                                        elif isinstance(data_json, dict) and "message" in data_json:
                                            # 有些API可能使用 "message" 字段
                                            msg = data_json["message"]
                                            if isinstance(msg, dict) and "content" in msg:
                                                accumulated_text += msg["content"]
                                            elif isinstance(msg, str):
                                                accumulated_text += msg
                                        elif isinstance(data_json, dict) and "done" in data_json and data_json.get("done"):
                                            # 流式响应结束标记
                                            pass
                                    except json.JSONDecodeError:
                                        # 如果不是JSON，直接累积文本
                                        accumulated_text += data_content
                            elif line_str.strip():  # 非空行
                                # 非SSE格式的行，直接累积
                                accumulated_text += line_str
                            
                            # 立即转发给WebUI（不等待完整响应）
                            self.wfile.write(line + b'\n')
                            self.wfile.flush()
                            
                            # 实时检测JSON指令（每累积一定内容就检查一次）
                            if accumulated_text and not json_sent and len(accumulated_text) > 50:
                                # 尝试提取JSON
                                json_data = forwarder.json_extractor.extract_json(accumulated_text)
                                if json_data and forwarder.json_extractor.validate_command(json_data):
                                    json_sent = True  # 标记已发送，避免重复
                                    def forward_command():
                                        logging.info("从ollama响应中检测到JSON指令，正在转发到机器狗...")
                                        success, result = forwarder.dog_controller.send_command(json_data)
                                        if success:
                                            task_id = result.get("task_id") if result else None
                                            logging.info(f"✓ 指令已发送到机器狗，任务ID: {task_id}")
                                        else:
                                            error = result.get("error") if result else "未知错误"
                                            logging.error(f"✗ 指令发送失败: {error}")
                                    
                                    threading.Thread(target=forward_command, daemon=True).start()
                    
                    # 如果流式响应结束时还没有检测到JSON，最后再检查一次完整内容
                    if accumulated_text and not json_sent:
                        json_data = forwarder.json_extractor.extract_json(accumulated_text)
                        if json_data and forwarder.json_extractor.validate_command(json_data):
                            logging.info("从ollama响应中检测到JSON指令，正在转发到机器狗...")
                            success, result = forwarder.dog_controller.send_command(json_data)
                            if success:
                                task_id = result.get("task_id") if result else None
                                logging.info(f"✓ 指令已发送到机器狗，任务ID: {task_id}")
                            else:
                                error = result.get("error") if result else "未知错误"
                                logging.error(f"✗ 指令发送失败: {error}")
                
                except Exception as e:
                    logging.error(f"处理请求时出错: {e}")
                    try:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
                    except:
                        pass
            
            def do_GET(self):
                """代理GET请求"""
                try:
                    ollama_endpoint = f"{ollama_url}{self.path}"
                    response = requests.get(ollama_endpoint, timeout=30)
                    
                    self.send_response(response.status_code)
                    for header, value in response.headers.items():
                        if header.lower() not in ['content-encoding', 'transfer-encoding', 'connection']:
                            self.send_header(header, value)
                    self.send_header('Connection', 'keep-alive')
                    self.end_headers()
                    self.wfile.write(response.content)
                except Exception as e:
                    logging.error(f"处理GET请求时出错: {e}")
                    try:
                        self.send_response(500)
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
                    except:
                        pass
            
            def log_message(self, format, *args):
                """禁用默认日志"""
                pass
        
        return ProxyHandler


class LLMForwarder:
    """大模型输出转发器"""
    
    def __init__(
        self,
        dog_ip: str,
        dog_user: str = "root",
        http_port: int = 8000,
        udp_port: int = 43893,
        ssh_port: int = 22,
        passwords: Optional[list] = None,
        ollama_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.dog_controller = DogController(dog_ip, dog_user, http_port, udp_port, ssh_port, passwords)
        self.json_extractor = JSONExtractor()
        self.running = True
        self._ollama_url = ollama_url or "http://localhost:11434"
        self._model = model or "qwen3:4b"
        
        # 注册信号处理（Windows上可能不支持SIGTERM）
        signal.signal(signal.SIGINT, self._signal_handler)
        if hasattr(signal, 'SIGTERM'):
            signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """处理退出信号"""
        logging.info("\n收到退出信号，正在清理...")
        self.running = False
        self.dog_controller.stop_server()
        sys.exit(0)
    
    def call_ollama_api(self, prompt: str, stream: bool = True) -> str:
        """调用Ollama API获取响应（支持流式输出）"""
        try:
            api_url = f"{self._ollama_url}/api/generate"
            payload = {
                "model": self._model,
                "prompt": prompt,
                "stream": stream
            }
            
            if stream:
                # 流式输出：实时显示大模型的响应
                logging.debug(f"正在请求Ollama API: {api_url}, 模型: {self._model}")
                response = requests.post(api_url, json=payload, timeout=300, stream=True)  # 增加超时时间到5分钟
                response.raise_for_status()
                
                full_response = ""
                print()  # 换行，使输出更清晰
                
                # 解析SSE格式的流式响应
                line_count = 0
                for line in response.iter_lines():
                    line_count += 1
                    if line:
                        # 确保解码为字符串（处理bytes和str两种情况）
                        if isinstance(line, bytes):
                            line_str = line.decode('utf-8', errors='ignore').strip()
                        else:
                            line_str = str(line).strip()
                        
                        # 跳过空行
                        if not line_str:
                            continue
                        
                        # Ollama的流式输出格式：每行是一个JSON对象（可能以data:开头，也可能直接是JSON）
                        try:
                            # 尝试去掉 'data: ' 前缀（如果有）
                            if line_str.startswith('data: '):
                                json_str = line_str[6:].strip()
                            else:
                                json_str = line_str
                            
                            # 跳过结束标记
                            if json_str == '[DONE]' or json_str == 'done':
                                break
                            
                            # 解析JSON
                            data = json.loads(json_str)
                            
                            # 提取响应片段（过滤thinking字段，只显示response）
                            if "response" in data:
                                chunk = data["response"]
                                if chunk:  # 只处理非空响应
                                    full_response += chunk
                                    # 实时显示，不换行（使用end=''）
                                    print(chunk, end='', flush=True)
                            
                            # 检查是否完成
                            if data.get("done", False):
                                break
                            
                            # 检查是否有错误
                            if "error" in data:
                                error_msg = data.get("error", "未知错误")
                                logging.error(f"Ollama API返回错误: {error_msg}")
                                print(f"\n[错误] {error_msg}")
                                break
                                
                        except json.JSONDecodeError as e:
                            # 如果不是JSON格式，可能是其他信息，记录但不中断
                            if line_count <= 3:  # 只记录前几行的解析错误
                                logging.debug(f"跳过非JSON行: {line_str[:50]}")
                            continue
                        except Exception as e:
                            # 记录错误但不中断，继续处理下一行
                            if line_count <= 10:  # 只记录前10行的错误
                                logging.debug(f"解析响应行时出错: {e}, 行内容: {line_str[:100]}")
                            continue
                
                print("\n")  # 流式输出结束后换行
                
                if not full_response:
                    logging.warning("流式输出未收到任何响应内容")
                
                return full_response
            else:
                # 非流式输出（兼容旧代码）
                logging.debug(f"正在请求Ollama API: {api_url}, 模型: {self._model}")
                response = requests.post(api_url, json=payload, timeout=300)  # 增加超时时间到5分钟
                response.raise_for_status()
                
                result = response.json()
                # 提取响应文本
                if "response" in result:
                    return result["response"]
                else:
                    return str(result)
        except requests.exceptions.Timeout as e:
            logging.error(f"调用Ollama API超时（可能模型响应时间过长）: {e}")
            return ""
        except requests.exceptions.ConnectionError as e:
            logging.error(f"无法连接到Ollama API ({self._ollama_url}): {e}")
            logging.error("请确保Ollama服务正在运行")
            return ""
        except requests.exceptions.HTTPError as e:
            logging.error(f"Ollama API返回HTTP错误: {e}")
            if hasattr(e.response, 'text'):
                logging.error(f"错误详情: {e.response.text[:200]}")
            return ""
        except requests.exceptions.RequestException as e:
            logging.error(f"调用Ollama API失败: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return ""
        except Exception as e:
            logging.error(f"调用Ollama API时发生未知错误: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return ""
    
    def start_interactive(self) -> bool:
        """启动交互式命令行界面"""
        # 1. 启动机器狗上的监听程序
        if not self.dog_controller.start_server():
            logging.error("无法启动机器狗监听程序，程序退出")
            return False
        
        logging.info("=" * 60)
        logging.info("转发程序已启动")
        logging.info(f"Ollama地址: {self._ollama_url}")
        logging.info(f"模型: {self._model}")
        logging.info("=" * 60)
        logging.info("提示：输入自然语言请求，程序会自动调用大模型并转发JSON指令")
        logging.info("输入 'exit' 或 'quit' 退出程序")
        logging.info("=" * 60)
        logging.info("")
        
        try:
            while self.running:
                try:
                    # 获取用户输入
                    user_input = input("> ").strip()
                    
                    if not user_input:
                        continue
                    
                    # 检查退出命令
                    if user_input.lower() in ['exit', 'quit', 'q']:
                        logging.info("正在退出...")
                        break
                    
                    # 调用Ollama API（流式输出，实时显示）
                    logging.info("正在调用大模型...")
                    response_text = self.call_ollama_api(user_input, stream=True)
                    
                    if not response_text:
                        logging.warning("大模型未返回响应")
                        continue
                    
                    # 提取JSON指令
                    json_data = self.json_extractor.extract_json(response_text)
                    if json_data and self.json_extractor.validate_command(json_data):
                        logging.info("检测到JSON指令，正在转发到机器狗...")
                        success, result = self.dog_controller.send_command(json_data)
                        
                        if success:
                            task_id = result.get("task_id") if result else None
                            logging.info(f"✓ 指令已发送到机器狗，任务ID: {task_id}")
                        else:
                            error = result.get("error") if result else "未知错误"
                            logging.error(f"✗ 指令发送失败: {error}")
                    else:
                        logging.info("响应中未检测到有效的JSON指令")
                    
                    print()  # 空行分隔
                    
                except EOFError:
                    # 用户按Ctrl+D（Unix）或Ctrl+Z（Windows）
                    logging.info("\n正在退出...")
                    break
                except KeyboardInterrupt:
                    # 用户按Ctrl+C
                    logging.info("\n正在退出...")
                    break
                except Exception as e:
                    logging.error(f"处理请求时出错: {e}")
                    import traceback
                    traceback.print_exc()
        
        finally:
            self.dog_controller.stop_server()
            logging.info("程序已退出")
        
        return True
    
    def start(self, watch_file: Optional[str] = None) -> bool:
        """启动转发程序"""
        self._watch_file = watch_file
        
        # 1. 启动机器狗上的监听程序
        if not self.dog_controller.start_server():
            logging.error("无法启动机器狗监听程序，程序退出")
            return False
        
        logging.info("=" * 60)
        logging.info("转发程序已启动，正在监听大模型输出...")
        logging.info("提示：大模型输出的JSON指令会自动转发到机器狗执行")
        logging.info("按 Ctrl+C 停止程序")
        logging.info("=" * 60)
        
        # 2. 开始监听（根据参数选择监听方式）
        try:
            if self._watch_file:
                self._listen_file()
            elif self._ollama_url:
                # 检查是否指定了 --listen-direct 模式
                if hasattr(self, '_listen_direct') and self._listen_direct:
                    # 直接监听模式（轮询API，功能受限）
                    self._listen_ollama_direct()
                else:
                    # 默认：启动ollama API透明代理（位于WebUI和Ollama之间）
                    self._ollama_proxy = OllamaAPIProxy(self, self._ollama_url, self._proxy_port)
                    self._ollama_proxy.start()
                    logging.info("")
                    logging.info("=" * 60)
                    logging.info("代理服务器运行中，等待WebUI请求...")
                    logging.info("提示：如果不想使用代理，可以使用 --watch-file 监听Ollama日志文件")
                    logging.info("=" * 60)
                    logging.info("")
                    # 保持运行
                    while self.running:
                        time.sleep(1)
            else:
                self._listen_stdin()
        except KeyboardInterrupt:
            logging.info("\n收到中断信号...")
        finally:
            if self._ollama_proxy:
                self._ollama_proxy.stop()
            self.dog_controller.stop_server()
            logging.info("程序已退出")
        
        return True
    
    def _listen_file(self):
        """监听文件变化"""
        if not os.path.exists(self._watch_file):
            logging.warning(f"文件不存在: {self._watch_file}，等待文件创建...")
        
        last_size = 0
        if os.path.exists(self._watch_file):
            last_size = os.path.getsize(self._watch_file)
        
        buffer = ""
        
        while self.running:
            try:
                if not os.path.exists(self._watch_file):
                    time.sleep(1)
                    continue
                
                current_size = os.path.getsize(self._watch_file)
                if current_size > last_size:
                    # 读取新增内容
                    with open(self._watch_file, 'r', encoding='utf-8', errors='ignore') as f:
                        f.seek(last_size)
                        new_content = f.read()
                        buffer += new_content
                        last_size = current_size
                        
                        # 尝试提取JSON
                        if buffer.strip():
                            json_data = self.json_extractor.extract_json(buffer)
                            if json_data and self.json_extractor.validate_command(json_data):
                                logging.info("检测到JSON指令，正在转发...")
                                success, result = self.dog_controller.send_command(json_data)
                                
                                if success:
                                    task_id = result.get("task_id") if result else None
                                    logging.info(f"✓ 指令已发送，任务ID: {task_id}")
                                else:
                                    error = result.get("error") if result else "未知错误"
                                    logging.error(f"✗ 指令发送失败: {error}")
                                
                                # 清空缓冲区（已处理）
                                buffer = ""
                            else:
                                # 如果缓冲区太长，清空一部分
                                if len(buffer) > 10000:
                                    buffer = buffer[-5000:]
                
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"监听文件时出错: {e}")
                time.sleep(1)
    
    def _listen_ollama_direct(self):
        """直接监听Ollama API（通过轮询最近响应）"""
        if not self._ollama_url:
            logging.error("未指定Ollama URL")
            return
        
        logging.info(f"直接监听Ollama API: {self._ollama_url}")
        logging.info("提示：此模式会定期检查Ollama的响应，无需修改WebUI配置")
        logging.info("注意：此模式可能无法实时捕获所有响应，建议使用 --watch-file 监听日志文件")
        
        last_checked_time = time.time()
        processed_responses = set()  # 记录已处理的响应ID（如果有）
        
        while self.running:
            try:
                # 尝试获取Ollama的对话历史（如果API支持）
                # 注意：Ollama的API可能不直接提供历史记录，这里只是示例
                # 实际实现可能需要监听日志文件或使用其他方法
                
                # 由于Ollama API不直接提供历史记录查询，这个方案不太可行
                # 建议用户使用 --watch-file 监听日志文件
                logging.warning("直接监听Ollama API功能受限，建议使用 --watch-file 监听日志文件")
                time.sleep(5)
                
            except Exception as e:
                logging.error(f"监听Ollama API时出错: {e}")
                time.sleep(2)
    
    def _listen_stdin(self):
        """监听标准输入"""
        buffer = ""
        
        while self.running:
            try:
                # 非阻塞读取（如果支持）
                if sys.stdin.isatty():
                    # 交互式终端：逐行读取
                    line = sys.stdin.readline()
                    if not line:
                        break
                    buffer += line
                else:
                    # 管道输入：批量读取
                    chunk = sys.stdin.read(1024)
                    if not chunk:
                        break
                    buffer += chunk
                
                # 尝试提取JSON
                if buffer.strip():
                    json_data = self.json_extractor.extract_json(buffer)
                    if json_data and self.json_extractor.validate_command(json_data):
                        logging.info("检测到JSON指令，正在转发...")
                        success, result = self.dog_controller.send_command(json_data)
                        
                        if success:
                            task_id = result.get("task_id") if result else None
                            logging.info(f"✓ 指令已发送，任务ID: {task_id}")
                        else:
                            error = result.get("error") if result else "未知错误"
                            logging.error(f"✗ 指令发送失败: {error}")
                        
                        # 清空缓冲区（已处理）
                        buffer = ""
                    else:
                        # 如果缓冲区太长，清空一部分（避免内存占用）
                        if len(buffer) > 10000:
                            buffer = buffer[-5000:]  # 保留最后5000字符
                
                # 短暂休眠，避免CPU占用过高
                time.sleep(0.1)
                
            except Exception as e:
                logging.error(f"读取输入时出错: {e}")
                time.sleep(0.5)
    
    def forward_from_text(self, text: str) -> bool:
        """从文本中提取并转发指令（用于API调用场景）"""
        json_data = self.json_extractor.extract_json(text)
        if json_data and self.json_extractor.validate_command(json_data):
            logging.info("检测到JSON指令，正在转发...")
            success, result = self.dog_controller.send_command(json_data)
            
            if success:
                task_id = result.get("task_id") if result else None
                logging.info(f"✓ 指令已发送，任务ID: {task_id}")
                return True
            else:
                error = result.get("error") if result else "未知错误"
                logging.error(f"✗ 指令发送失败: {error}")
                return False
        return False


def find_ollama_log_file() -> Optional[str]:
    """自动查找Ollama日志文件位置"""
    possible_paths = []
    
    # Windows路径
    if os.name == 'nt':
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        if local_appdata:
            possible_paths.extend([
                os.path.join(local_appdata, 'ollama', 'logs', 'server.log'),
                os.path.join(local_appdata, 'ollama', 'logs', 'ollama.log'),
            ])
    
    # Linux/Mac路径
    else:
        possible_paths.extend([
            os.path.expanduser('~/.ollama/logs/server.log'),
            os.path.expanduser('~/.ollama/logs/ollama.log'),
            '/var/log/ollama/server.log',
            '/var/log/ollama/ollama.log',
        ])
    
    # 检查文件是否存在
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    return None


def main():
    parser = argparse.ArgumentParser(
        description="大模型输出转发程序：交互式命令行界面，调用ollama API并转发JSON指令到机器狗"
    )
    parser.add_argument(
        "--dog-ip",
        required=True,
        help="机器狗IP地址（例如：192.168.1.100）"
    )
    parser.add_argument(
        "--dog-user",
        default="root",
        help="SSH用户名（默认：root）"
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=8000,
        help="机器狗HTTP服务端口（默认：8000）"
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=43893,
        help="机器狗UDP端口（默认：43893）"
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=22,
        help="SSH端口（默认：22）"
    )
    parser.add_argument(
        "--passwords",
        nargs="+",
        default=["1", "root"],
        help="SSH密码列表，按顺序尝试（默认：1 root）"
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama API地址（默认：http://localhost:11434）"
    )
    parser.add_argument(
        "--model",
        default="qwen3:4b",
        help="Ollama模型名称（默认：qwen3:4b）"
    )
    
    args = parser.parse_args()
    
    forwarder = LLMForwarder(
        dog_ip=args.dog_ip,
        dog_user=args.dog_user,
        http_port=args.http_port,
        udp_port=args.udp_port,
        ssh_port=args.ssh_port,
        passwords=args.passwords,
        ollama_url=args.ollama_url,
        model=args.model,
    )
    
    # 启动交互式模式
    forwarder.start_interactive()


if __name__ == "__main__":
    main()
