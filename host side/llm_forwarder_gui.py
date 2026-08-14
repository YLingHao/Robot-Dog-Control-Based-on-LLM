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

import csv
import logging
import queue
import threading
import time
import os
import tempfile
from datetime import datetime
from typing import Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

from action_kb import build_augmented_prompt
from llm_forwarder import LLMForwarder, JSONExtractor, JSONPipeline


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
        self.root.geometry("1040x760")

        # 当前 forwarder 实例（每次启动/终止可以重建）
        self._forwarder: Optional[LLMForwarder] = None
        self._running = False
        self._dog_log_index = 0  # 机器狗日志的起始索引
        self._dog_log_timer = None  # 日志轮询定时器

        # 语音录制 / Whisper 相关状态
        self._whisper_model = None          # 延迟加载的 Whisper small 模型
        self._recording = False             # 是否正在录音
        self._recording_thread = None       # 录音后台线程
        self._recording_frames = []         # 录音采样数据列表
        self._recording_fs = 16000          # 采样率（Whisper 推荐 16k）

        # 唤醒词监听相关
        self._wake_listening = False        # 是否正在监听唤醒词
        self._wake_thread = None            # 唤醒词监听线程

        # 延迟测试相关状态
        self._latency_measure_active = False
        self._latency_t0 = None
        self._latency_marks = {}
        self._latency_meta = {}
        self._latency_csv_path = os.path.abspath("latency_results_gui.csv")

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
        self.entry_dog_ip.insert(0, "192.168.1.100")
        self.entry_dog_ip.grid(row=0, column=1, sticky="w", padx=4, pady=3)

        ttk.Label(cfg_frame, text="SSH 密码(可空):").grid(row=0, column=2, sticky="e", padx=4, pady=3)
        self.entry_password = ttk.Entry(cfg_frame, width=18, show="*")
        self.entry_password.insert(0, "1")
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
                "qwen3-dog",
                "qwen3:4b",
                "qwen3:4b-instruct",
                "qwen3-4b-instruct-new",
                "qwen2.5:7b",
                "llama3:8b",
                "deepseek-r1:7b",
            ],
        )
        self.combo_model.set("qwen3-4b-instruct-new")
        self.combo_model.grid(row=1, column=3, sticky="w", padx=4, pady=3)

        # 行 3：按钮
        btn_frame = ttk.Frame(cfg_frame)
        btn_frame.grid(row=2, column=0, columnspan=4, sticky="w", pady=5)

        self.btn_start = ttk.Button(btn_frame, text="启动", width=10, command=self.on_start)
        self.btn_start.pack(side=tk.LEFT, padx=4)

        self.btn_stop = ttk.Button(btn_frame, text="终止", width=10, command=self.on_stop, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        self.btn_emergency = ttk.Button(btn_frame, text="急停", width=10, command=self.on_emergency_stop, state=tk.DISABLED)
        self.btn_emergency.pack(side=tk.LEFT, padx=4)

        self.btn_clear_history = ttk.Button(btn_frame, text="清空历史", width=10, command=self.on_clear_history)
        self.btn_clear_history.pack(side=tk.LEFT, padx=4)

        self.test_mode_var = tk.BooleanVar(value=False)
        self.chk_test_mode = ttk.Checkbutton(btn_frame, text="测试模式", variable=self.test_mode_var)
        self.chk_test_mode.pack(side=tk.LEFT, padx=4)

        self.latency_test_var = tk.BooleanVar(value=False)
        self.chk_latency_test = ttk.Checkbutton(btn_frame, text="延迟测试", variable=self.latency_test_var)
        self.chk_latency_test.pack(side=tk.LEFT, padx=4)

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

        # 语音输入按钮（按一次开始录音，再按一次结束录音）
        self.btn_voice = ttk.Button(send_frame, text="语音", width=8, command=self.on_voice_button)
        self.btn_voice.pack(side=tk.TOP, pady=2)

        # 语音自动发送开关
        self.auto_send_var = tk.BooleanVar(value=False)
        self.chk_auto_send = ttk.Checkbutton(send_frame, text="语音自动发送", variable=self.auto_send_var)
        self.chk_auto_send.pack(side=tk.TOP, pady=2)

        # 自动录音（唤醒词）开关
        self.auto_record_var = tk.BooleanVar(value=False)
        self.chk_auto_record = ttk.Checkbutton(send_frame, text="自动录音", variable=self.auto_record_var, command=self.on_auto_record_toggle)
        self.chk_auto_record.pack(side=tk.TOP, pady=2)

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
        model = self.combo_model.get().strip() or "qwen3-4b-instruct-new"
        user_pwd = self.entry_password.get().strip()

        # 构造密码列表：如果用户填了，就优先用用户密码
        if user_pwd:
            passwords = [user_pwd, "1", "root"]
        else:
            passwords = ["1", "root"]

        test_mode = self.test_mode_var.get()

        # 禁用启动按钮，启用终止、急停和发送
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_emergency.config(state=tk.NORMAL)
        self.btn_send.config(state=tk.NORMAL)

        self._running = True

        def worker():
            try:
                mode_text = "测试模式" if test_mode else "机器狗连接模式"
                logging.info(f"=== 正在创建转发器（{mode_text}） ===")
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

                if test_mode:
                    logging.info("=== 已进入测试模式：不连接机器狗，仅测试主机端解析与界面功能 ===")
                    return

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
            self.btn_emergency.config(state=tk.DISABLED)
            self.btn_send.config(state=tk.DISABLED)

        self.root.after(0, _reset)

    def on_clear_history(self) -> None:
        """清空当前显示内容"""
        self.text_request.delete("1.0", tk.END)
        self.text_model_output.delete("1.0", tk.END)
        self.text_think.delete("1.0", tk.END)
        self.text_final.delete("1.0", tk.END)
        logging.info("已清空当前输入和输出显示")
        messagebox.showinfo("提示", "当前输入和输出已清空。")

    def on_stop(self) -> None:
        self._running = False
        logging.info("已停止监听服务")

        # 停止机器狗日志轮询
        self._stop_dog_log_polling()

        def worker():
            try:
                if self._forwarder is not None:
                    if self.test_mode_var.get():
                        logging.info("=== 正在退出测试模式 ===")
                        self._forwarder = None
                        logging.info("=== 测试模式已停止，界面仍可再次启动 ===")
                    else:
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
                        self.btn_emergency.config(state=tk.DISABLED),
                        self.btn_send.config(state=tk.DISABLED),
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def on_emergency_stop(self) -> None:
        if not self._forwarder or not self._running:
            messagebox.showwarning("提示", "请先启动并连接机器狗。")
            return

        if self.test_mode_var.get():
            logging.warning("测试模式下不会向机器狗发送急停命令。")
            messagebox.showinfo("提示", "测试模式下不会发送急停命令。")
            return

        self.btn_emergency.config(state=tk.DISABLED)

        def worker():
            try:
                logging.warning("=== 正在执行急停：当前动作与后续队列将被取消 ===")
                ok, result = self._forwarder.dog_controller.emergency_stop()
                if ok:
                    logging.warning("✓ 急停命令已发送，机器狗当前动作和后续动作已被终止。")
                else:
                    err = result.get("error") if result else "未知错误"
                    logging.error(f"✗ 急停失败: {err}")
            finally:
                self.root.after(0, lambda: self.btn_emergency.config(state=tk.NORMAL if self._running else tk.DISABLED))

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

    # ------------------------------------------------------------------
    # 语音输入 / Whisper 集成
    # ------------------------------------------------------------------
    def _ensure_whisper_model(self):
        """延迟加载 Whisper small 模型。若未安装则给出友好提示。"""
        if self._whisper_model is not None:
            return self._whisper_model

        try:
            import whisper  # type: ignore
        except ImportError:
            msg = (
                "未检测到 Whisper 库，无法进行语音转文本。\n\n"
                "请在本机终端中执行以下命令安装（在 pytorch 虚拟环境里）：\n"
                "  pip install -U openai-whisper\n\n"
                "并确保已安装 ffmpeg。"
            )
            logging.error("未安装 whisper 库，语音转文本功能不可用。")
            self.root.after(0, lambda: messagebox.showerror("缺少依赖", msg))
            return None

        logging.info("正在加载 Whisper small 模型（首次加载可能需要数十秒）...")
        try:
            model = whisper.load_model("small")
        except Exception as e:
            logging.error(f"加载 Whisper small 模型失败: {e}")
            self.root.after(0, lambda: messagebox.showerror("错误", f"加载 Whisper 模型失败：{e}"))
            return None

        self._whisper_model = model
        logging.info("Whisper small 模型加载完成。")
        return self._whisper_model

    def on_auto_record_toggle(self) -> None:
        """自动录音开关切换：启动或停止唤醒词监听线程。"""
        if self.auto_record_var.get():
            self._wake_listening = True
            self._wake_thread = threading.Thread(target=self._wake_word_loop, daemon=True)
            self._wake_thread.start()
            logging.info("自动录音已启用，等待唤醒词'小狗小狗'...")
        else:
            self._wake_listening = False
            logging.info("自动录音已禁用。")

    def _wake_word_loop(self) -> None:
        """后台持续监听唤醒词，检测到'小狗'后触发录音。"""
        try:
            import sounddevice as sd
            import numpy as np
            import tempfile
            import os
            from scipy.io.wavfile import write as wav_write
        except ImportError:
            logging.error("唤醒词监听需要 sounddevice/scipy，请先安装。")
            self.root.after(0, lambda: self.auto_record_var.set(False))
            return

        model = self._ensure_whisper_model()
        if model is None:
            logging.error("唤醒词监听：Whisper 模型加载失败，自动录音已停用。")
            self.root.after(0, lambda: self.auto_record_var.set(False))
            return

        logging.info("Whisper 模型就绪，开始监听唤醒词...")

        fs = self._recording_fs
        WAKE_WORDS = ["小狗", "小狗小狗"]
        SPEECH_THRESHOLD = 500   # 触发识别的音量阈值
        COLLECT_SECONDS = 2.0    # 检测到声音后收集的音频时长
        BLOCK_SIZE = int(fs * 0.1)  # 每次回调 0.1 秒

        audio_buffer = []
        collecting = [False]
        collect_frames = [0]
        COLLECT_BLOCKS = int(COLLECT_SECONDS / 0.1)

        def callback(indata, frames, time_info, status):
            if not self._wake_listening or self._recording:
                return
            chunk = indata.copy()
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            if not collecting[0]:
                if rms > SPEECH_THRESHOLD:
                    # 检测到声音，开始收集
                    collecting[0] = True
                    collect_frames[0] = 0
                    audio_buffer.clear()
            if collecting[0]:
                audio_buffer.append(chunk)
                collect_frames[0] += 1

        logging.info("唤醒词监听已启动（持续监听），说'小狗小狗'触发录音...")
        try:
            with sd.InputStream(samplerate=fs, channels=1, dtype='int16',
                                blocksize=BLOCK_SIZE, callback=callback):
                while self._wake_listening:
                    time.sleep(0.1)
                    if self._recording:
                        # 正在录音，暂停收集
                        collecting[0] = False
                        audio_buffer.clear()
                        continue
                    if collecting[0] and collect_frames[0] >= COLLECT_BLOCKS:
                        # 收集完毕，进行识别
                        collecting[0] = False
                        frames_data = np.concatenate(audio_buffer, axis=0)
                        audio_buffer.clear()
                        tmp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                                tmp_path = f.name
                            wav_write(tmp_path, fs, frames_data)
                            result = model.transcribe(tmp_path, language="zh", fp16=False)
                            text = result.get("text", "").strip()
                            logging.debug(f"唤醒词检测: {text}")
                            if any(w in text for w in WAKE_WORDS):
                                logging.info(f"检测到唤醒词: {text}，触发录音！")
                                import winsound
                                winsound.Beep(1000, 300)
                                self.root.after(0, self.on_voice_button)
                        except Exception as e:
                            logging.error(f"唤醒词识别出错: {e}")
                        finally:
                            if tmp_path:
                                try:
                                    os.unlink(tmp_path)
                                except Exception:
                                    pass
        except Exception as e:
            logging.error(f"唤醒词监听出错: {e}")

    def on_voice_button(self) -> None:
        """语音按钮：第一次点击开始录音，再次点击结束录音并转写。"""
        # 若正在录音，则本次点击为“停止并转写”
        if self._recording:
            logging.info("结束录音，准备进行语音转文本...")
            self._recording = False
            # 按钮先禁用，等转写结束再恢复
            self.btn_voice.config(state=tk.DISABLED, text="语音")
            return

        # 未在录音，则开始录音
        def start_recording():
            # 延迟导入录音依赖
            try:
                import sounddevice as sd  # type: ignore
            except ImportError:
                msg = (
                    "未检测到录音相关库，无法从麦克风录音。\n\n"
                    "请在本机终端中执行以下命令安装（在 pytorch 虚拟环境里）：\n"
                    "  pip install -U sounddevice scipy\n"
                )
                logging.error("未安装 sounddevice/scipy，语音录音功能不可用。")
                self.root.after(0, lambda: messagebox.showerror("缺少依赖", msg))
                return

            self._recording_frames = []
            self._recording = True

            def audio_worker():
                import numpy as np  # type: ignore
                from scipy.io.wavfile import write as wav_write  # type: ignore

                try:
                    fs = self._recording_fs
                    SILENCE_THRESHOLD = 500
                    SILENCE_DURATION = 1.5
                    last_speech_time = [time.time()]
                    speech_detected = [False]

                    def callback(indata, frames, time_info, status):
                        if status:
                            logging.warning(f"录音状态警告: {status}")
                        self._recording_frames.append(indata.copy())
                        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
                        if rms > SILENCE_THRESHOLD:
                            last_speech_time[0] = time.time()
                            speech_detected[0] = True

                    import sounddevice as sd  # type: ignore
                    logging.info("开始录音：停止说话后自动结束，或再次点击\u201c语音\u201d按钮手动结束。")
                    with sd.InputStream(
                        samplerate=fs,
                        channels=1,
                        dtype="int16",
                        callback=callback,
                    ):
                        while self._recording:
                            sd.sleep(100)
                            if speech_detected[0] and (time.time() - last_speech_time[0]) > SILENCE_DURATION:
                                logging.info("检测到说话停止，自动结束录音...")
                                self._recording = False
                                self.root.after(0, lambda: self.btn_voice.config(text="语音"))
                                break
                except Exception as e:
                    logging.error(f"录音过程中出错: {e}")
                    self.root.after(
                        0, lambda: messagebox.showerror("录音失败", f"录音失败：{e}")
                    )
                    self.root.after(
                        0, lambda: self.btn_voice.config(state=tk.NORMAL, text="语音")
                    )
                    return

                # 录音结束，开始转写
                if not self._recording_frames:
                    logging.warning("未采集到任何音频样本。")
                    self.root.after(
                        0,
                        lambda: (
                            messagebox.showinfo("提示", "没有录到有效声音，请重试。"),
                            self.btn_voice.config(state=tk.NORMAL, text="语音"),
                        ),
                    )
                    return

                # 拼接所有帧
                try:
                    import numpy as np  # type: ignore

                    audio_data = np.concatenate(self._recording_frames, axis=0)
                except Exception as e:
                    logging.error(f"拼接录音数据失败: {e}")
                    self.root.after(
                        0, lambda: self.btn_voice.config(state=tk.NORMAL, text="语音")
                    )
                    return

                model = self._ensure_whisper_model()
                if model is None:
                    self.root.after(
                        0, lambda: self.btn_voice.config(state=tk.NORMAL, text="语音")
                    )
                    return

                if self.latency_test_var.get():
                    self._latency_start(mode="audio")

                logging.info("录音结束，正在保存临时音频文件并调用 Whisper 转文本...")

                tmp_path = None
                try:
                    from scipy.io.wavfile import write as wav_write  # type: ignore

                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                        tmp_path = f.name
                        wav_write(tmp_path, self._recording_fs, audio_data)

                    # 调用 Whisper 进行中文转写（显式指定简体中文风格）
                    try:
                        result = model.transcribe(
                            tmp_path,
                            language="zh",
                            task="transcribe",
                            fp16=False,
                            initial_prompt="请使用简体中文输出，不要使用繁体字。以下是机器狗控制命令，常见词汇包括：前进、后退、左转、右转、左平移、右平移、打招呼、扭身体、扭身跳、后空翻、向前跳、翻身、米、度、停止。",
                        )
                    except Exception as e:
                        logging.error(f"Whisper 转写失败: {e}")
                        self.root.after(
                            0,
                            lambda: messagebox.showerror(
                                "转写失败", f"Whisper 转写失败：{e}"
                            ),
                        )
                        return

                    text = (result.get("text") or "").strip()

                    # 尝试将繁体转换为简体（如果系统安装了 opencc，则自动使用）
                    try:
                        from opencc import OpenCC  # type: ignore

                        conv = OpenCC("t2s")
                        text = conv.convert(text)
                    except Exception:
                        # 没装 opencc 或转换失败时，直接使用原文
                        pass
                    if not text:
                        logging.warning("Whisper 未识别出有效文本。")
                        self.root.after(
                            0,
                            lambda: messagebox.showinfo(
                                "提示", "未识别到有效语音内容，请重试。"
                            ),
                        )
                        return

                    logging.info(f"Whisper 识别结果: {text}")
                    if self._latency_measure_active:
                        self._latency_meta["asr_text"] = text
                        self._latency_mark("asr_done")

                    def update_input_and_maybe_send():
                        # 将识别结果填入输入框（覆盖原内容）
                        self.text_request.delete("1.0", tk.END)
                        self.text_request.insert(tk.END, text)

                        # 根据开关决定是否自动发送
                        if self.auto_send_var.get():
                            if self._forwarder and self._running:
                                logging.info("语音识别完成，自动发送到大模型。")
                                self.on_send()
                            else:
                                logging.warning("当前未启动机器狗监听服务，自动发送已跳过。")

                        # 恢复按钮状态
                        self.btn_voice.config(state=tk.NORMAL, text="语音")

                    self.root.after(0, update_input_and_maybe_send)
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.remove(tmp_path)
                        except Exception:
                            pass

            # 启动录音后台线程
            self._recording_thread = threading.Thread(
                target=audio_worker, daemon=True
            )
            self._recording_thread.start()

        # 切换到“录音中”状态
        self._recording = True
        self.btn_voice.config(text="停止")
        threading.Thread(target=start_recording, daemon=True).start()

    def on_send(self) -> None:
        if not self._forwarder or not self._running:
            messagebox.showwarning("提示", "请先启动并连接机器狗。")
            return

        if self.latency_test_var.get() and not self._latency_measure_active:
            self._latency_start(mode="text")

        prompt = self.text_request.get("1.0", tk.END).strip()
        if self._latency_measure_active:
            self._latency_meta["input_text"] = prompt
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
                logging.info("开始调用大模型（流式输出，单轮解析）...")
                if self._latency_measure_active:
                    self._latency_mark("llm_start")
                full_text = self._call_ollama_stream_gui(prompt)
                if self._latency_measure_active:
                    self._latency_mark("llm_done")

                if not full_text:
                    logging.warning("大模型未返回任何内容。")
                else:
                    # full_text 已经是过滤掉 think 后的纯 response 内容
                    # 直接显示到最终输出区域
                    self._append_text_safe(self.text_final, full_text + "\n")

                    # 从最终文本中提取 JSON 指令并转发
                    logging.debug(f"用于JSON提取的文本: {full_text}")
                    if self._latency_measure_active:
                        self._latency_mark("json_start")
                    json_data, post_fixes, (json_valid, json_reason) = JSONPipeline.extract_repair_and_validate(
                        full_text,
                        prompt,
                    )
                    if post_fixes:
                        logging.info(f"JSON 后修复: {post_fixes}")
                    if self._latency_measure_active:
                        self._latency_mark("json_done")
                    if json_data and json_valid:
                        logging.info(f"JSON 校验通过，原因: {json_reason}")
                        if self.test_mode_var.get():
                            logging.info("测试模式下检测到有效 JSON 指令，已跳过机器狗发送。")
                            logging.info(f"测试模式 JSON: {json_data}")
                            if self._latency_measure_active:
                                self._latency_log_summary(None)
                        else:
                            logging.info("从大模型输出中检测到 JSON 指令，正在转发到机器狗...")
                            if self._latency_measure_active:
                                self._latency_mark("submit_start")
                            ok, result = self._forwarder.dog_controller.send_command(json_data)
                            if self._latency_measure_active:
                                self._latency_mark("submit_done")
                            if ok:
                                task_id = result.get("task_id") if result else None
                                if self._latency_measure_active:
                                    self._latency_meta["task_id"] = task_id or ""
                                logging.info(f"✓ 指令已发送到机器狗，任务ID: {task_id}")
                                if self._latency_measure_active and task_id:
                                    threading.Thread(
                                        target=self._poll_task_result_for_latency,
                                        args=(task_id,),
                                        daemon=True
                                    ).start()
                            else:
                                err = result.get("error") if result else "未知错误"
                                if self._latency_measure_active:
                                    self._latency_meta["error"] = err
                                logging.error(f"✗ 指令发送失败: {err}")
                                if self._latency_measure_active:
                                    self._latency_log_summary(None)
                    else:
                        preview = full_text[:300].replace("\n", "\\n")
                        logging.info(f"本次大模型输出中未检测到有效的 JSON 指令，原因: {json_reason}。")
                        logging.info(f"模型输出摘要: {preview}")
                        if json_data:
                            logging.info(f"提取后的候选 JSON: {json_data}")
                        if self._latency_measure_active:
                            self._latency_meta["json_reason"] = json_reason
                            self._latency_log_summary(None)
            finally:
                self.root.after(0, lambda: self.btn_send.config(state=tk.NORMAL))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # 大模型调用（GUI 版流式输出，单轮解析）
    # ------------------------------------------------------------------
    def _call_ollama_stream_gui(self, prompt: str) -> str:
        """
        参照 LLMForwarder.call_ollama_api 的流式实现，但输出到 GUI。
        使用 /api/generate 接口进行单轮解析，并在请求前注入本地动作知识库。
        """
        import requests
        import json

        context_prompt, kb_debug = build_augmented_prompt(prompt)
        logging.info(
            "构造单轮聊天请求: retrieve_ms=%sms%s",
            kb_debug.get("retrieve_ms", 0),
            " (fallback)" if kb_debug.get("used_fallback") else "",
        )

        api_url = f"{self._forwarder._ollama_url}/api/chat"
        
        # 根据思考模式开关设置参数（默认不启用思考模式）
        enable_thinking = False
        payload = {
            "model": self._forwarder._model,
            "messages": [
                {"role": "system", "content": kb_debug.get("system_instruction", "")},
                {"role": "user", "content": prompt}
            ],
            "stream": True,
        }

        payload["options"] = {
            "Think": False,
            "temperature": 0.95,
            "top_p": 0.7,
            "num_predict": 512
        }

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
                thinking_chunk = None
                if "thinking" in data:
                    thinking_chunk = data["thinking"]
                    if thinking_chunk:
                        full_thinking += thinking_chunk
                        # 实时显示think内容到think区域（带[思考]标记，逐字显示）
                        self._append_text_safe(self.text_think, f"[思考] {thinking_chunk}\n")
                        # 实时显示think内容到模型输出区域（不带[思考]标记，累积显示）
                        self._append_text_safe(self.text_model_output, thinking_chunk)
                        thinking_displayed_to_model = True

                response_chunk = None
                message = data.get("message")
                if isinstance(message, dict):
                    response_chunk = message.get("content")
                if response_chunk is None and "response" in data:
                    response_chunk = data["response"]
                if response_chunk is not None:
                    if full_thinking and not thinking_displayed_to_model:
                        self._append_text_safe(self.text_model_output, full_thinking)
                        thinking_displayed_to_model = True

                    full_response += response_chunk
                    self._append_text_safe(self.text_model_output, response_chunk)

                if data.get("done", False):
                    # 如果结束时还有think内容但没显示到模型输出窗口，显示它
                    if full_thinking and not thinking_displayed_to_model:
                        self._append_text_safe(self.text_model_output, full_thinking)
                    # 确保最后有一个换行
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
    def _latency_reset(self) -> None:
        self._latency_measure_active = False
        self._latency_t0 = None
        self._latency_marks = {}
        self._latency_meta = {}

    def _latency_start(self, mode: str = "text") -> None:
        self._latency_measure_active = True
        self._latency_t0 = time.perf_counter()
        self._latency_marks = {"t0": self._latency_t0}
        self._latency_meta = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "input_text": "",
            "asr_text": "",
            "task_id": "",
            "task_status": "",
            "error": "",
        }

    def _latency_mark(self, name: str) -> None:
        if self._latency_measure_active:
            self._latency_marks[name] = time.perf_counter()

    def _latency_ms(self, start_key: str, end_key: str) -> float:
        if start_key in self._latency_marks and end_key in self._latency_marks:
            return round((self._latency_marks[end_key] - self._latency_marks[start_key]) * 1000.0, 2)
        return -1.0

    def _write_latency_csv(self, row: dict) -> None:
        fieldnames = [
            "timestamp",
            "mode",
            "input_text",
            "asr_text",
            "task_id",
            "task_status",
            "asr_latency_ms",
            "llm_latency_ms",
            "json_latency_ms",
            "http_submit_latency_ms",
            "host_total_ms",
            "end_to_end_response_ms",
            "server_queue_wait_ms",
            "server_exec_ms",
            "server_total_ms",
            "task_total_ms",
            "error",
        ]
        file_exists = os.path.exists(self._latency_csv_path)
        with open(self._latency_csv_path, "a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

    def _latency_log_summary(self, task_info: dict | None = None) -> None:
        if not self._latency_measure_active:
            return

        self._latency_mark("done")

        asr_ms = self._latency_ms("t0", "asr_done")
        llm_ms = self._latency_ms("llm_start", "llm_done")
        json_ms = self._latency_ms("json_start", "json_done")
        submit_ms = self._latency_ms("submit_start", "submit_done")
        # 主机侧总耗时：只统计到“提交完成”为止，不包含机器狗端等待与执行时间
        host_total_ms = self._latency_ms("t0", "submit_done") if "submit_done" in self._latency_marks else self._latency_ms("t0", "json_done")
        # 任务总时长：从开始计时到动作执行完毕
        task_total_ms = self._latency_ms("t0", "done")

        queue_wait_ms = None
        exec_ms = None
        server_total_ms = None
        end_to_end_response_ms = None
        task_status = ""
        task_id = self._latency_meta.get("task_id", "")
        error = self._latency_meta.get("error", "")

        logging.info("=== 延迟测试结果 ===")
        if asr_ms >= 0:
            logging.info(f"语音识别延迟: {asr_ms} ms")
        if llm_ms >= 0:
            logging.info(f"模型推理延迟: {llm_ms} ms")
        if json_ms >= 0:
            logging.info(f"JSON提取与校验延迟: {json_ms} ms")
        if submit_ms >= 0:
            logging.info(f"指令发送延迟: {submit_ms} ms")
        if host_total_ms >= 0:
            logging.info(f"主机侧总耗时: {host_total_ms} ms")

        if task_info:
            task_status = task_info.get("status", "")
            created_at = task_info.get("created_at")
            started_at = task_info.get("started_at")
            finished_at = task_info.get("finished_at")
            if created_at and started_at:
                queue_wait_ms = round((started_at - created_at) * 1000.0, 2)
                logging.info(f"机器狗端队列等待时间: {queue_wait_ms} ms")
            if started_at and finished_at:
                exec_ms = round((finished_at - started_at) * 1000.0, 2)
                logging.info(f"机器狗端执行时间: {exec_ms} ms")
            if created_at and finished_at:
                server_total_ms = round((finished_at - created_at) * 1000.0, 2)
                logging.info(f"机器狗端总耗时: {server_total_ms} ms")

        # 端到端响应延迟：到机器狗“开始执行”为止，不包含动作完整执行时长
        if host_total_ms >= 0 and queue_wait_ms is not None:
            end_to_end_response_ms = round(host_total_ms + queue_wait_ms, 2)
            logging.info(f"端到端响应延迟: {end_to_end_response_ms} ms")
        elif host_total_ms >= 0:
            end_to_end_response_ms = host_total_ms

        if task_total_ms >= 0:
            logging.info(f"任务总时长: {task_total_ms} ms")

        row = {
            "timestamp": self._latency_meta.get("timestamp", ""),
            "mode": self._latency_meta.get("mode", ""),
            "input_text": self._latency_meta.get("input_text", ""),
            "asr_text": self._latency_meta.get("asr_text", ""),
            "task_id": task_id,
            "task_status": task_status,
            "asr_latency_ms": asr_ms if asr_ms >= 0 else "",
            "llm_latency_ms": llm_ms if llm_ms >= 0 else "",
            "json_latency_ms": json_ms if json_ms >= 0 else "",
            "http_submit_latency_ms": submit_ms if submit_ms >= 0 else "",
            "host_total_ms": host_total_ms if host_total_ms >= 0 else "",
            "end_to_end_response_ms": end_to_end_response_ms if end_to_end_response_ms is not None else "",
            "server_queue_wait_ms": queue_wait_ms if queue_wait_ms is not None else "",
            "server_exec_ms": exec_ms if exec_ms is not None else "",
            "server_total_ms": server_total_ms if server_total_ms is not None else "",
            "task_total_ms": task_total_ms if task_total_ms >= 0 else "",
            "error": error,
        }
        self._write_latency_csv(row)
        logging.info(f"延迟测试结果已写入 CSV: {self._latency_csv_path}")
        logging.info("===================")
        self._latency_reset()

    def _poll_task_result_for_latency(self, task_id: str, poll_interval: float = 0.1, timeout: float = 120.0) -> None:
        import requests

        if not self._forwarder:
            return

        deadline = time.time() + timeout
        url = f"http://{self._forwarder.dog_controller.dog_ip}:{self._forwarder.dog_controller.http_port}/result?task_id={task_id}"

        while time.time() < deadline:
            try:
                resp = requests.get(url, timeout=5)
                resp.raise_for_status()
                data = resp.json()
                task = data.get("task")
                if task and task.get("status") in ["done", "failed", "cancelled"]:
                    self._latency_log_summary(task)
                    return
            except Exception:
                pass
            time.sleep(poll_interval)

        logging.warning("延迟测试：任务结果轮询超时")
        self._latency_log_summary(None)

    def _append_text_safe(self, widget: tk.Text, msg: str) -> None:
        widget.after(0, lambda: (widget.insert(tk.END, msg), widget.see(tk.END)))

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    gui = ForwarderGUI()
    gui.run()


if __name__ == "__main__":
    main()

