#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""机器狗端常驻HTTP服务（零依赖）。

特性：
- 监听 0.0.0.0
- POST /execute : 提交动作序列JSON，返回 task_id
- GET  /result?task_id=... : 查询执行结果/状态
- POST /emergency_stop : 立即急停（抢占），并取消队列中未开始的任务
- GET  /health : 健康检查

关键增强：
- 执行动作任务在子进程中运行：即使子进程崩溃/退出，HTTP服务主进程也保持可用，不会出现 Connection refused

实现约束：
- 不依赖任何第三方库（无互联网也能用）

注意：
- 子进程会单独创建UDP socket/状态监听端口，因此同一时间只允许一个任务 running。
"""

import io
import json
import logging
import multiprocessing as mp
import queue
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")

# 日志收集器：收集所有日志输出
class LogCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._logs: list = []
        self._max_logs = 1000  # 最多保存1000条日志
        
    def append(self, log_entry: str):
        with self._lock:
            self._logs.append(log_entry)
            # 保持日志数量在限制内
            if len(self._logs) > self._max_logs:
                self._logs = self._logs[-self._max_logs:]
    
    def get_logs(self, since: int = 0) -> list:
        """获取日志，since 是起始索引"""
        with self._lock:
            return self._logs[since:]
    
    def clear(self):
        with self._lock:
            self._logs.clear()

# 全局日志收集器
_log_collector = LogCollector()

# 用于子进程传递日志的队列
_log_queue: Optional[mp.Queue] = None

# 自定义日志处理器，将日志输出到收集器
class LogCollectorHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        # 不过滤，所有日志都收集
        _log_collector.append(log_entry)
        # 如果是子进程，也发送到队列
        if _log_queue is not None:
            try:
                _log_queue.put_nowait(("log", log_entry))
            except:
                pass

# 添加日志收集器到根 logger
_log_handler = LogCollectorHandler()
_log_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_log_handler)

# 用于捕获 print() 和 stderr 的流包装器
class LogStream(io.TextIOBase):
    """将 stdout/stderr 输出转换为日志的流包装器"""
    def __init__(self, name: str, original_stream, log_queue: Optional["mp.Queue"] = None):
        self.name = name
        self.original_stream = original_stream
        self.log_queue = log_queue
        self.buffer = []  # 用于缓冲不完整的行
        
    def write(self, text: str) -> int:
        # 捕获所有输出（包括空行和多行输出）
        if text:
            # 将新文本添加到缓冲区
            self.buffer.append(text)
            
            # 检查是否有完整的行（以换行符结尾）
            buffered_text = ''.join(self.buffer)
            if '\n' in buffered_text:
                # 按行分割
                lines = buffered_text.split('\n')
                # 最后一部分（可能不完整）保留在缓冲区
                self.buffer = [lines[-1]] if lines[-1] else []
                
                # 处理完整的行
                for line in lines[:-1]:
                    line = line.rstrip('\r')
                    # 发送每一行到日志队列（添加时间戳，与logging格式一致）
                    if self.log_queue is not None:
                        try:
                            import datetime
                            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            # 如果行不为空，添加时间戳；空行也保留（某些库用空行作为分隔）
                            formatted_line = f"[{timestamp}] INFO {line}" if line else ""
                            if formatted_line:
                                self.log_queue.put_nowait(("print", formatted_line))
                        except:
                            pass
        
        return len(text)
    
    def flush(self):
        # 刷新缓冲区中的内容
        if self.buffer:
            buffered_text = ''.join(self.buffer)
            if buffered_text.strip():
                if self.log_queue is not None:
                    try:
                        import datetime
                        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        formatted_line = f"[{timestamp}] INFO {buffered_text.rstrip()}"
                        self.log_queue.put_nowait(("print", formatted_line))
                    except:
                        pass
            self.buffer = []
        
        if self.original_stream:
            try:
                self.original_stream.flush()
            except:
                pass


def _worker_run(task_id: str, payload: Dict[str, Any], dog_ip: str, dog_port: int, result_queue: "mp.Queue", log_queue: "mp.Queue") -> None:
    """子进程执行入口。"""
    global _log_queue
    _log_queue = log_queue
    
    # 在子进程中设置日志收集器，将日志发送到队列
    # 清除所有现有的 handler，避免日志重复输出
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    class WorkerLogHandler(logging.Handler):
        def emit(self, record):
            log_entry = self.format(record)
            try:
                log_queue.put_nowait(("log", log_entry))
            except:
                pass
    
    worker_handler = WorkerLogHandler()
    worker_handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root_logger.addHandler(worker_handler)
    root_logger.setLevel(logging.INFO)  # 设置日志级别
    
    # 重定向 stdout 和 stderr 到日志流
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = LogStream("stdout", original_stdout, log_queue)
    sys.stderr = LogStream("stderr", original_stderr, log_queue)
    
    try:
        # 直接导入命令执行器（不再做依赖库检查）
        from dog_llm_exec import DogCommandExecutor  # 子进程内导入

        executor = DogCommandExecutor(dog_ip, dog_port)
        try:
            res = executor.exec_actions(payload)
            out = {
                "ok": all(r.ok for r in res) and len(res) == len(payload.get("actions", [])),
                "results": [
                    {
                        "ok": r.ok,
                        "index": r.action_index,
                        "code": hex(r.code),
                        "param": r.param,
                        "message": r.message,
                        "started_at": r.started_at,
                        "finished_at": r.finished_at,
                        "duration": round(r.finished_at - r.started_at, 3),
                    }
                    for r in res
                ],
            }
            result_queue.put({"task_id": task_id, "status": "done", "result": out, "error": None})
        finally:
            try:
                executor.close()
            except Exception:
                pass
    except Exception as e:
        # 子进程内异常（包括导入失败）
        try:
            result_queue.put({"task_id": task_id, "status": "failed", "result": None, "error": str(e)})
        except Exception:
            pass
    finally:
        # 确保所有日志都被发送（刷新缓冲区）
        try:
            if hasattr(sys.stdout, 'flush'):
                sys.stdout.flush()
            if hasattr(sys.stderr, 'flush'):
                sys.stderr.flush()
        except:
            pass
        
        # 恢复原始 stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        
        # 发送一个结束标记，确保主进程知道子进程已退出
        try:
            log_queue.put(("done", ""))
        except:
            pass


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create(self, payload: Dict[str, Any]) -> str:
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "status": "queued",  # queued/running/done/failed/cancelled
                "payload": payload,
                "result": None,
                "error": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "worker_exitcode": None,
            }
        return task_id

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t.update(kwargs)

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            t = self._tasks.get(task_id)
            return dict(t) if t else None

    def cancel_all_queued(self, reason: str) -> None:
        now = time.time()
        with self._lock:
            for t in self._tasks.values():
                if t.get("status") == "queued":
                    t["status"] = "cancelled"
                    t["error"] = reason
                    t["finished_at"] = now


class CommandService:
    def __init__(self, dog_ip: str, dog_port: int) -> None:
        self._dog_ip = dog_ip
        self._dog_port = dog_port

        self._tasks = TaskStore()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()

        self._current_proc: Optional[mp.Process] = None
        self._current_task_id: Optional[str] = None
        self._current_log_queue: Optional["mp.Queue"] = None
        self._result_queue: "mp.Queue" = mp.Queue()

        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def submit(self, payload: Dict[str, Any]) -> str:
        task_id = self._tasks.create(payload)
        self._queue.put(task_id)
        return task_id

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._tasks.get(task_id)

    def emergency_stop(self) -> None:
        # 取消队列
        self._tasks.cancel_all_queued("被急停取消")
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

        # 终止当前子进程
        if self._current_proc is not None and self._current_proc.is_alive():
            self._current_proc.terminate()

        # 尝试直接对运动主机发送急停（主进程内快速执行）
        try:
            import socket
            import struct

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(struct.pack('<3i', 0x21020C0E, 0, 0), (self._dog_ip, self._dog_port))
            s.close()
        except Exception:
            pass

    def _drain_worker_results(self) -> None:
        while True:
            try:
                msg = self._result_queue.get_nowait()
            except Exception:
                return

            task_id = msg.get("task_id")
            status = msg.get("status")
            result = msg.get("result")
            error = msg.get("error")
            self._tasks.update(task_id, status=status, result=result, error=error, finished_at=time.time())

    def _drain_log_queue(self) -> None:
        """处理子进程日志队列，将日志添加到主进程的日志收集器"""
        if self._current_log_queue is None:
            return
        try:
            # 使用超时机制，避免无限等待
            max_iterations = 100  # 每次最多处理100条，避免阻塞
            count = 0
            while count < max_iterations:
                try:
                    # 使用超时避免阻塞
                    msg_type, log_entry = self._current_log_queue.get(timeout=0.01)
                    if msg_type in ("log", "print"):
                        # 将子进程的日志添加到主进程的日志收集器
                        _log_collector.append(log_entry)
                    count += 1
                except queue.Empty:
                    break
                except Exception:
                    break
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._drain_worker_results()
            self._drain_log_queue()  # 处理日志队列

            # 回收当前子进程
            if self._current_proc is not None and not self._current_proc.is_alive():
                # 处理剩余的日志（多次处理确保不遗漏）
                for _ in range(10):  # 最多处理10轮，确保所有日志都被收集
                    self._drain_log_queue()
                    if self._current_log_queue is None:
                        break
                    # 短暂等待，让子进程有时间发送最后的日志
                    time.sleep(0.05)
                
                exitcode = self._current_proc.exitcode
                if self._current_task_id:
                    # 如果子进程异常退出且没上报结果，则标记failed
                    t = self._tasks.get(self._current_task_id)
                    if t and t.get("status") == "running":
                        self._tasks.update(
                            self._current_task_id,
                            status="failed",
                            error=f"子进程异常退出 exitcode={exitcode}",
                            finished_at=time.time(),
                            worker_exitcode=exitcode,
                        )
                    else:
                        self._tasks.update(self._current_task_id, worker_exitcode=exitcode)

                self._current_proc = None
                self._current_task_id = None
                self._current_log_queue = None

            # 有任务在跑就不启动新任务
            if self._current_proc is not None:
                time.sleep(0.05)
                continue

            try:
                task_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            t = self._tasks.get(task_id)
            if not t or t.get("status") != "queued":
                continue

            payload = t.get("payload") or {}
            self._tasks.update(task_id, status="running", started_at=time.time())

            # 创建子进程专用的日志队列
            log_queue = mp.Queue()
            
            proc = mp.Process(
                target=_worker_run,
                args=(task_id, payload, self._dog_ip, self._dog_port, self._result_queue, log_queue),
                daemon=True,
            )
            proc.start()
            self._current_proc = proc
            self._current_task_id = task_id
            self._current_log_queue = log_queue


_SERVICE: Optional[CommandService] = None


class Handler(BaseHTTPRequestHandler):
    server_version = "DogLLMExec/0.3"

    def _send_json(self, code: int, body: Dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        length_str = self.headers.get("Content-Length")
        raw: bytes
        if length_str is None or length_str.strip() == "" or length_str.strip() == "0":
            raw = self.rfile.read()
        else:
            length = int(length_str)
            raw = self.rfile.read(length) if length > 0 else b""

        if not raw:
            return {}

        text = raw.decode("utf-8", errors="ignore").strip()
        if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
            text = text[1:-1]
        return json.loads(text)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        global _SERVICE
        try:
            if self.path == "/execute":
                payload = self._read_json()
                if not payload:
                    self._send_json(400, {"ok": False, "error": "空请求体或JSON解析失败"})
                    return
                task_id = _SERVICE.submit(payload)  # type: ignore
                self._send_json(200, {"ok": True, "task_id": task_id})
                return

            if self.path == "/emergency_stop":
                _SERVICE.emergency_stop()  # type: ignore
                self._send_json(200, {"ok": True, "message": "已急停"})
                return

            self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def do_GET(self) -> None:
        global _SERVICE
        try:
            if self.path == "/health":
                self._send_json(200, {"ok": True})
                return

            if self.path.startswith("/result"):
                task_id = None
                if "?" in self.path:
                    _, q = self.path.split("?", 1)
                    for part in q.split("&"):
                        if part.startswith("task_id="):
                            task_id = part.split("=", 1)[1]
                if not task_id:
                    self._send_json(400, {"ok": False, "error": "missing task_id"})
                    return
                t = _SERVICE.get_task(task_id)  # type: ignore
                if not t:
                    self._send_json(404, {"ok": False, "error": "task not found"})
                    return
                self._send_json(200, {"ok": True, "task": t})
                return

            if self.path.startswith("/logs"):
                # 获取日志，支持 since 参数（起始索引）
                since = 0
                if "?" in self.path:
                    _, q = self.path.split("?", 1)
                    for part in q.split("&"):
                        if part.startswith("since="):
                            try:
                                since = int(part.split("=", 1)[1])
                            except ValueError:
                                pass
                logs = _log_collector.get_logs(since)
                self._send_json(200, {"ok": True, "logs": logs, "count": len(logs), "since": since})
                return

            self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        # 完全跳过 /logs 轮询请求的日志记录（避免刷屏），这些请求本身是用于获取日志的，不需要记录
        if "/logs" in message:
            return
        log_msg = "%s - %s" % (self.address_string(), message)
        logging.info(log_msg)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="0.0.0.0", help="监听地址，默认0.0.0.0")
    p.add_argument("--port", type=int, default=8000, help="HTTP端口，默认8000")
    p.add_argument("--dog-ip", default="192.168.1.120", help="UDP目标IP，默认192.168.1.120")
    p.add_argument("--dog-port", type=int, default=43893, help="UDP目标端口，默认43893")
    args = p.parse_args()

    global _SERVICE
    _SERVICE = CommandService(args.dog_ip, args.dog_port)

    httpd = ThreadingHTTPServer((args.listen, args.port), Handler)
    logging.info(f"HTTP服务已启动: http://{args.listen}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    # 在 davinci-mini 这类环境上强制 fork 可能导致主进程不稳定/崩溃。
    # 使用 spawn 更保守：子进程完全重新启动解释器，避免继承主进程的网络/线程状态。
    mp.set_start_method('spawn', force=True)
    main()
