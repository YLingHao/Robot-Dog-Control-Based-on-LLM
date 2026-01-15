#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""llm_forwarder 图形界面封装

功能概述：
- 在界面中填写：机器狗 IP、连接密码（可留空使用默认密码列表）、Ollama 地址、本地模型名；
- 「启动」按钮：通过 SSH 连接机器狗，自动执行
      cd /root/opt/dog_llm_exec/ && python dog_llm_exec_server.py
  启动狗端监听服务，连接过程与结果日志显示在界面日志区；
- 在界面中输入对大模型的请求，点击「发送」：
  - 实时在「模型输出」区展示大模型的流式输出；
  - 在「思考(think)」区单独展示 think 内容（如果模型没有 think 就保持为空）；
  - 在「最终输出」区展示过滤 think 后的最终文本；
  - 自动从最终输出中提取 JSON 指令并经 HTTP 转发给机器狗，结果写入日志；
- 「终止」按钮：停止狗端监听服务，但不关闭本界面，可再次点击「启动」重新连接。
"""

import logging
import queue
import threading
import time
from typing import Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

from llm_forwarder import LLMForwarder, JSONExtractor


class TkLogHandler(logging.Handler):
    """将 logging 输出重定向到 Tkinter 文本框的 Handler。"""

    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record) + "\n"
        # 通过 after 让 UI 线程安全更新
        self.text_widget.after(0, self._append, msg)

    def _append(self, msg: str) -> None:
        self.text_widget.insert(tk.END, msg)
        self.text_widget.see(tk.END)


class ForwarderGUI:
    """llm_forwarder 的简单图形界面封装。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("机器狗 LLM 监听转发程序")
        self.root.geometry("980x720")

        # 当前 forwarder 实例（每次启动/终止可以重建）
        self._forwarder: Optional[LLMForwarder] = None
        self._running = False
        self._dog_log_index = 0  # 机器狗日志的起始索引
        self._dog_log_timer = None  # 日志轮询定时器

        # UI 组件
        self._build_widgets()

        # 日志重定向
        self._install_logging_handler()

    # ------------------------------------------------------------------
    # UI 搭建
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        # 顶部配置区域
        cfg_frame = ttk.LabelFrame(self.root, text="连接与模型配置")
        cfg_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=5)

        # 行 1：狗 IP + 密码
        ttk.Label(cfg_frame, text="机器狗 IP:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.entry_dog_ip = ttk.Entry(cfg_frame, width=18)
        self.entry_dog_ip.grid(row=0, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(cfg_frame, text="SSH 密码(可空):").grid(row=0, column=2, sticky="e", padx=4, pady=3)
        self.entry_password = ttk.Entry(cfg_frame, width=18, show="*")
        self.entry_password.grid(row=0, column=3, sticky="w", padx=4, pady=3)

        # 行 2：Ollama URL + 模型
        ttk.Label(cfg_frame, text="Ollama 地址:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.entry_ollama = ttk.Entry(cfg_frame, width=28)
        self.entry_ollama.insert(0, "http://localhost:11434")
        self.entry_ollama.grid(row=1, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(cfg_frame, text="模型:").grid(row=1, column=2, sticky="e", padx=4, pady=3)
        self.combo_model = ttk.Combobox(
            cfg_frame,
            width=20,
            values=[
                "qwen3:4b",
                "qwen2.5:7b",
                "llama3:8b",
                "deepseek-r1:7b",
            ],
        )
        self.combo_model.set("qwen3:4b")
        self.combo_model.grid(row=1, column=3, sticky="w", padx=4, pady=3)

        # 行 3：按钮
        btn_frame = ttk.Frame(cfg_frame)
        btn_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=5)

        self.btn_start = ttk.Button(btn_frame, text="启动", width=10, command=self.on_start)
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_stop = ttk.Button(btn_frame, text="终止", width=10, command=self.on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        # 中部：对话与输出
        mid_frame = ttk.Frame(self.root)
        mid_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=5)

        # 左侧：对话输入 + 模型输出
        left_frame = ttk.Frame(mid_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 请求输入
        req_frame = ttk.LabelFrame(left_frame, text="向大模型发送请求")
        req_frame.pack(side=tk.TOP, fill=tk.X, pady=4)

        self.text_request = tk.Text(req_frame, height=4)
        self.text_request.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=4)

        send_frame = ttk.Frame(req_frame)
        send_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=4)

        self.btn_send = ttk.Button(send_frame, text="发送", width=8, command=self.on_send, state=tk.DISABLED)
        self.btn_send.pack(side=tk.TOP, pady=2)

        # 模型输出（流式原始输出）
        out_frame = ttk.LabelFrame(left_frame, text="模型输出（原始，含 think 内容）")
        out_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=4)

        self.text_model_output = tk.Text(out_frame, height=12)
        self.text_model_output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        scroll_out = ttk.Scrollbar(out_frame, command=self.text_model_output.yview)
        scroll_out.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_model_output.configure(yscrollcommand=scroll_out.set)

        # 右侧：think + 最终输出 + 日志
        right_frame = ttk.Frame(mid_frame)
        right_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # think 区
        think_frame = ttk.LabelFrame(right_frame, text="思考 (think)")
        think_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=4)

        self.text_think = tk.Text(think_frame, height=8, foreground="#666666")
        self.text_think.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        scroll_think = ttk.Scrollbar(think_frame, command=self.text_think.yview)
        scroll_think.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_think.configure(yscrollcommand=scroll_think.set)

        # 最终输出
        final_frame = ttk.LabelFrame(right_frame, text="最终输出（过滤 think 后）")
        final_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=4)

        self.text_final = tk.Text(final_frame, height=8)
        self.text_final.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)

        scroll_final = ttk.Scrollbar(final_frame, command=self.text_final.yview)
        scroll_final.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_final.configure(yscrollcommand=scroll_final.set)

        # 底部：主机日志 + 机器狗日志（分左右两栏）
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=8, pady=5)
        
        # 左侧：主机日志
        host_log_frame = ttk.LabelFrame(bottom_frame, text="主机日志")
        host_log_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        
        self.text_log = tk.Text(host_log_frame, height=8)
        self.text_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)
        
        scroll_log = ttk.Scrollbar(host_log_frame, command=self.text_log.yview)
        scroll_log.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_log.configure(yscrollcommand=scroll_log.set)
        
        # 右侧：机器狗日志
        dog_log_frame = ttk.LabelFrame(bottom_frame, text="机器狗日志")
        dog_log_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
        
        self.text_dog_log = tk.Text(dog_log_frame, height=8, foreground="#0066cc")
        self.text_dog_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4, pady=4)
        
        scroll_dog_log = ttk.Scrollbar(dog_log_frame, command=self.text_dog_log.yview)
        scroll_dog_log.pack(side=tk.RIGHT, fill=tk.Y)
        self.text_dog_log.configure(yscrollcommand=scroll_dog_log.set)

    def _install_logging_handler(self) -> None:
        handler = TkLogHandler(self.text_log)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        dog_ip = self.entry_dog_ip.get().strip()
        if not dog_ip:
            messagebox.showwarning("提示", "请先填写机器狗 IP 地址。")
            return

        ollama_url = self.entry_ollama.get().strip() or "http://localhost:11434"
        model = self.combo_model.get().strip() or "qwen3:4b"
        user_pwd = self.entry_password.get().strip()

        # 构造密码列表：如果用户填了，就优先用用户密码
        if user_pwd:
            passwords = [user_pwd, "1", "root"]
        else:
            passwords = ["1", "root"]

        # 禁用启动按钮，启用终止和发送
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_send.config(state=tk.NORMAL)

        self._running = True

        def worker():
            try:
                logging.info("=== 正在创建转发器并连接机器狗 ===")
                forwarder = LLMForwarder(
                    dog_ip=dog_ip,
                    dog_user="root",
                    http_port=8000,
                    udp_port=43893,
                    ssh_port=22,
                    passwords=passwords,
                    ollama_url=ollama_url,
                    model=model,
                    enable_signal_handler=False,  # GUI 环境中禁用信号处理
                )
                self._forwarder = forwarder

                # 仅启动狗端监听服务，不进入命令行交互循环
                ok = forwarder.dog_controller.start_server()
                if not ok:
                    logging.error("无法启动机器狗监听程序，请检查日志。")
                    messagebox.showerror("错误", "启动机器狗端监听服务失败，请查看日志。")
                    self._forwarder = None
                    self._running = False
                    self._reset_buttons_after_error()
                    return

                logging.info("=== 机器狗监听服务已启动，可以在上方输入请求并点击\"发送\" ===")
                
                # 启动机器狗日志轮询
                self._start_dog_log_polling()
            except Exception as e:
                logging.error(f"启动过程中出现异常: {e}")
                messagebox.showerror("错误", f"启动失败：{e}")
                self._forwarder = None
                self._running = False
                self._reset_buttons_after_error()

        threading.Thread(target=worker, daemon=True).start()

    def _reset_buttons_after_error(self) -> None:
        def _reset():
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_send.config(state=tk.DISABLED)

        self.root.after(0, _reset)

    def on_stop(self) -> None:
        self._running = False
        
        # 停止机器狗日志轮询
        self._stop_dog_log_polling()

        def worker():
            try:
                if self._forwarder is not None:
                    logging.info("=== 正在停止机器狗监听服务 ===")
                    try:
                        self._forwarder.dog_controller.stop_server()
                    finally:
                        self._forwarder = None
                    logging.info("=== 监听服务已停止，界面仍可再次启动 ===")
            finally:
                self.root.after(
                    0,
                    lambda: (
                        self.btn_start.config(state=tk.NORMAL),
                        self.btn_stop.config(state=tk.DISABLED),
                        self.btn_send.config(state=tk.DISABLED),
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()
    
    def _start_dog_log_polling(self) -> None:
        """启动机器狗日志轮询"""
        self._dog_log_index = 0
        self._poll_dog_logs()
    
    def _stop_dog_log_polling(self) -> None:
        """停止机器狗日志轮询"""
        if self._dog_log_timer:
            self.root.after_cancel(self._dog_log_timer)
            self._dog_log_timer = None
    
    def _poll_dog_logs(self) -> None:
        """轮询机器狗日志"""
        if not self._running or not self._forwarder:
            return
        
        def fetch_logs():
            try:
                import requests
                dog_ip = self._forwarder.dog_controller.dog_ip
                http_port = self._forwarder.dog_controller.http_port
                url = f"http://{dog_ip}:{http_port}/logs?since={self._dog_log_index}"
                
                response = requests.get(url, timeout=2)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("ok") and data.get("logs"):
                        logs = data.get("logs", [])
                        if logs:
                            # 更新日志索引
                            self._dog_log_index += len(logs)
                            # 显示日志
                            for log_entry in logs:
                                self._append_text_safe(self.text_dog_log, log_entry + "\n")
            except Exception as e:
                # 静默处理错误，避免日志刷屏
                pass
        
        # 在后台线程获取日志
        threading.Thread(target=fetch_logs, daemon=True).start()
        
        # 每500ms轮询一次
        self._dog_log_timer = self.root.after(500, self._poll_dog_logs)

    def on_send(self) -> None:
        if not self._forwarder or not self._running:
            messagebox.showwarning("提示", "请先启动并连接机器狗。")
            return

        prompt = self.text_request.get("1.0", tk.END).strip()
        if not prompt:
            messagebox.showwarning("提示", "请输入要发送给大模型的内容。")
            return

        # 清空输出区
        self.text_model_output.delete("1.0", tk.END)
        self.text_think.delete("1.0", tk.END)
        self.text_final.delete("1.0", tk.END)

        self.btn_send.config(state=tk.DISABLED)

        def worker():
            try:
                logging.info("开始调用大模型（流式输出）...")
                full_text = self._call_ollama_stream_gui(prompt)

                if not full_text:
                    logging.warning("大模型未返回任何内容。")
                else:
                    # full_text 已经是过滤掉 think 后的纯 response 内容
                    # 直接显示到最终输出区域
                    self._append_text_safe(self.text_final, full_text + "\n")

                    # 从最终文本中提取 JSON 指令并转发
                    json_data = JSONExtractor.extract_json(full_text)
                    if json_data and JSONExtractor.validate_command(json_data):
                        logging.info("从大模型输出中检测到 JSON 指令，正在转发到机器狗...")
                        ok, result = self._forwarder.dog_controller.send_command(json_data)
                        if ok:
                            task_id = result.get("task_id") if result else None
                            logging.info(f"✓ 指令已发送到机器狗，任务ID: {task_id}")
                        else:
                            err = result.get("error") if result else "未知错误"
                            logging.error(f"✗ 指令发送失败: {err}")
                    else:
                        logging.info("本次大模型输出中未检测到有效的 JSON 指令。")
            finally:
                self.root.after(0, lambda: self.btn_send.config(state=tk.NORMAL))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # 大模型调用（GUI 版流式输出）
    # ------------------------------------------------------------------
    def _call_ollama_stream_gui(self, prompt: str) -> str:
        """参照 LLMForwarder.call_ollama_api 的流式实现，但输出到 GUI。"""
        api_url = f"{self._forwarder._ollama_url}/api/generate"
        payload = {
            "model": self._forwarder._model,
            "prompt": prompt,
            "stream": True,
        }

        import requests
        import json

        try:
            resp = requests.post(api_url, json=payload, timeout=300, stream=True)
            resp.raise_for_status()
        except Exception as e:
            logging.error(f"调用 Ollama API 失败: {e}")
            return ""

        full_response = ""  # 最终响应（不含think）
        full_thinking = ""   # think内容（累积，用于模型输出窗口）
        thinking_displayed_to_model = False  # 标记think内容是否已显示到模型输出窗口
        line_count = 0

        for raw_line in resp.iter_lines():
            line_count += 1
            if not raw_line:
                continue

            try:
                line_str = raw_line.decode("utf-8", errors="ignore").strip()
            except Exception:
                continue

            if not line_str:
                continue

            try:
                if line_str.startswith("data: "):
                    json_str = line_str[6:].strip()
                else:
                    json_str = line_str

                if json_str in ("[DONE]", "done"):
                    break

                data = json.loads(json_str)

                # 提取thinking字段（思考过程）
                if "thinking" in data:
                    thinking_chunk = data["thinking"]
                    if thinking_chunk:
                        full_thinking += thinking_chunk
                        # 实时显示think内容到think区域（带[思考]标记，逐字显示）
                        self._append_text_safe(self.text_think, f"[思考] {thinking_chunk}\n")
                        # 实时显示think内容到模型输出区域（不带[思考]标记，累积显示）
                        self._append_text_safe(self.text_model_output, thinking_chunk)
                        thinking_displayed_to_model = True

                if "response" in data:
                    chunk = data["response"]
                    if chunk:
                        # 如果之前有think内容但还没显示到模型输出窗口，先显示
                        if full_thinking and not thinking_displayed_to_model:
                            self._append_text_safe(self.text_model_output, full_thinking + "\n")
                            thinking_displayed_to_model = True
                        
                        full_response += chunk
                        # 实时显示response到模型输出区域
                        self._append_text_safe(self.text_model_output, chunk)

                if data.get("done", False):
                    # 如果结束时还有think内容但没显示到模型输出窗口，显示它
                    if full_thinking and not thinking_displayed_to_model:
                        self._append_text_safe(self.text_model_output, full_thinking + "\n")
                    break

                if "error" in data:
                    err_msg = data.get("error", "未知错误")
                    logging.error(f"Ollama API 返回错误: {err_msg}")
                    break
            except json.JSONDecodeError:
                if line_count <= 3:
                    logging.debug(f"跳过非 JSON 行: {line_str[:80]}")
                continue
            except Exception as e:
                if line_count <= 10:
                    logging.debug(f"解析流式响应时出错: {e}, 行内容: {line_str[:80]}")
                continue

        # 换行
        self._append_text_safe(self.text_model_output, "\n")
        
        # 返回完整文本（用于后续JSON提取）
        # 注意：最终输出窗口应该只包含 response，不包含 thinking
        # 所以返回时只返回 full_response，不包含 think 标记
        return full_response

    # ------------------------------------------------------------------
    # think 拆分逻辑：尽量复用 JSONExtractor.filter_think_content 的规则
    # ------------------------------------------------------------------
    def _split_think_and_content(self, text: str) -> Tuple[str, str]:
        """拆分 think 和非 think 内容。

        实现策略：
        1. 首先识别显式的 [思考] 标记（来自API的thinking字段）
        2. 然后使用 JSONExtractor.filter_think_content 处理其他think格式
        """
        think_parts = []
        response_parts = []
        
        # 方法1：识别 [思考] 标记（来自API的thinking字段）
        lines = text.splitlines()
        in_think_block = False
        current_think = []
        
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[思考]"):
                # 开始think块
                think_content = stripped[4:].strip()  # 去掉 "[思考]" 前缀
                if think_content:
                    current_think.append(think_content)
                in_think_block = True
            elif in_think_block:
                if stripped:
                    # think块的后续行
                    current_think.append(stripped)
                else:
                    # 空行结束think块
                    if current_think:
                        think_parts.append("\n".join(current_think))
                        current_think = []
                    in_think_block = False
                    response_parts.append(line)
            else:
                response_parts.append(line)
        
        # 处理最后一个think块（如果没有空行结尾）
        if current_think:
            think_parts.append("\n".join(current_think))
        
        # 如果找到了显式的think标记，直接返回
        if think_parts:
            think_text = "\n\n".join(think_parts)
            response_text = "\n".join(response_parts)
            return think_text, response_text
        
        # 方法2：使用 JSONExtractor.filter_think_content 处理其他格式
        filtered = JSONExtractor.filter_think_content(text)
        if filtered == text:
            # 没有明显 think 段落
            return "", text

        # 简单差分：按行对比
        orig_lines = text.splitlines()
        filtered_lines = filtered.splitlines()

        think_lines = []
        fi = 0

        for ol in orig_lines:
            if fi < len(filtered_lines) and ol == filtered_lines[fi]:
                fi += 1
            else:
                think_lines.append(ol)

        think_text = "\n".join(think_lines)
        return think_text, filtered

    # ------------------------------------------------------------------
    def _append_text_safe(self, widget: tk.Text, msg: str) -> None:
        widget.after(0, lambda: (widget.insert(tk.END, msg), widget.see(tk.END)))

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    gui = ForwarderGUI()
    gui.run()


if __name__ == "__main__":
    main()

