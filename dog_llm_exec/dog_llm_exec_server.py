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

import json
import logging
import multiprocessing as mp
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")


def _worker_run(task_id: str, payload: Dict[str, Any], dog_ip: str, dog_port: int, result_queue: "mp.Queue") -> None:
    """子进程执行入口。"""
    try:
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

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._drain_worker_results()

            # 回收当前子进程
            if self._current_proc is not None and not self._current_proc.is_alive():
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

            proc = mp.Process(
                target=_worker_run,
                args=(task_id, payload, self._dog_ip, self._dog_port, self._result_queue),
                daemon=True,
            )
            proc.start()
            self._current_proc = proc
            self._current_task_id = task_id


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

            self._send_json(404, {"ok": False, "error": "not found"})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s" % (self.address_string(), fmt % args))


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
