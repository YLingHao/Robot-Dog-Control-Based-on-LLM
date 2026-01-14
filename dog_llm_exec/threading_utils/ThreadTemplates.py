#!/usr/bin/env python
# -*- coding: utf-8 -*-

import threading
import time
import logging

# 这些 import 会在 dog_llm_exec.py 中被替换为本地导入
# from speeds.sportspeed import *
# from sendcommand import SendToCommand, heartbeat
# from socketnetwork import network_utils

# 配置日志
logging.basicConfig(level=logging.DEBUG)


class MyRepeatThread(threading.Thread):
    def __init__(self, name, action, interval, time_limit = None, *args) -> None:
        super(MyRepeatThread, self).__init__()
        self.name = name
        self.action = action
        self.interval = interval  # 发送命令的频率
        self.time_limit = time_limit  # 最大时间阈值
        self.args = args
        self.stopped = threading.Event()
        self.start_time = time.time()  # 记录线程开始的时间
        self.global_var = 0   
        self.current_time = time.time() # 当前时间
        self.current_time_start = 0


    def run(self) -> None:
        print(f"Starting {self.name}")
        while not self.stopped.is_set():
            self.current_time = time.time()
            
            # 如果超过时间阈值，则停止线程
            if self.time_limit is not None and self.check_time_and_stop(time.time()):
                break

            try:
                self.action(*self.args)  # 展开参数
            except KeyboardInterrupt:
                self.stopped = True
            except Exception as e:
                # 增加对 OSError 的捕获，避免 socket 关闭后线程崩溃
                if isinstance(e, OSError):
                    logging.warning(f"{self.name} 执行时发生OSError (socket可能已关闭), 线程停止。")
                    self.stopped.set()
                else:
                    logging.error(f"{self.name} 执行时发生异常: {e}")
                self.stopped.set()

            # 计算需要休眠多长时间以保持固定频率
            action_start_time = time.time()
            elapsed_time = action_start_time - self.current_time
            time_to_wait = max(0, self.interval - elapsed_time)
            time.sleep(time_to_wait)
        
        self.stopped.set()  # 确保线程停止状态被设置
        logging.info(f'离开线程：{self.name}')

    def check_time_and_stop(self, current_time) -> bool:
        if current_time - self.start_time > self.time_limit:
            logging.info(f'{self.name}由于超过时间阈值{self.time_limit}秒，系统自动停止！')
            self.stopped.set()
            return True
        elif self.global_var == 1:
            self.stopped.set()
            return True
        return False
    

    def stop(self) -> None:
        self.stopped.set()

    def print_attributes(self):
        """
        打印对象的所有属性及其值
        """
        self.current_time_start = time.time() - self.start_time
        return {attr: value for attr, value in vars(self).items()}

