#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""机器狗命令执行器 (V3 - 独立稳定版)

本次修改核心目标：
1.  **项目独立**：移除所有对外部 `1_*`, `2_*` 文件夹的 `sys.path` 依赖，
    所有模块（command, sendcommand, speeds 等）均从本项目目录导入。
2.  **心跳稳定**：使用新创建的 `sendcommand.heartbeat.send_udp_heartbeat_once`，
    避免了原函数因内部 `while/close` 导致的线程和socket不稳定问题。

下一步将在该稳定版本上，继续修复动作执行逻辑（如抬头低头、连续移动等）。
"""

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# ===================================================================
# 1. 项目内依赖导入 (不再依赖外部文件夹)
# ===================================================================
from command.udp_command import RobotState
from sendcommand.heartbeat import send_udp_heartbeat_once
from sendcommand.SendToCommand import perform_action
from socketnetwork import network_utils
from speeds.sportspeed import go_straight, translate_left_and_right, revolve_left_and_right
from threading_utils.ThreadTemplates import MyRepeatThread
from robotstatuswatcher.listener import status_listener


logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")


class DogState:
    UNKNOWN = "unknown"
    STANDING = "standing"
    LYING = "lying"


@dataclass
class ExecResult:
    ok: bool
    action_index: int
    code: int
    param: Any
    message: str
    started_at: float
    finished_at: float


# 动作前置状态定义（执行动作前需要达到的状态，参考gesture_main.py的设计）
PREREQUISITE_STATE: Dict[int, str] = {
    0x21010502: DogState.LYING,  # 后空翻
    0x2101050B: DogState.LYING,  # 向前跳
    0x21010205: DogState.LYING,  # 翻身
    0x21010507: DogState.STANDING,  # 打招呼（参考gesture_main.py第179行：从站立状态执行）
    0x21010204: DogState.STANDING,  # 扭身体（参考gesture_main.py第157行）
    0x2101030C: DogState.STANDING,  # 太空步（参考gesture_main.py第167行）
    0x2101020D: DogState.STANDING,  # 扭身跳（参考gesture_main.py第147行）
}

# 动作执行状态（参考gesture_main.py的状态监听设计）
# 格式: {动作码: [basic_state, gait_state, motion_state]}
ACTION_EXECUTION_STATE: Dict[int, List[int]] = {
    0x21010204: [6, 0, 2],   # 扭身体：执行状态 [6, 0, 2]
    0x2101020D: [6, 0, 4],   # 扭身跳：执行状态 [6, 0, 4]
    0x2101030C: [6, 12, 1],  # 太空步：执行状态 [6, 12, 1]
    0x21010507: [20, 0, 0],  # 打招呼：执行状态 [20, 0, 0]
    # 趴下类动作：执行后状态为 [1, 0, 0]
    0x21010502: [1, 0, 0],   # 后空翻：执行后状态 [1, 0, 0]
    0x2101050B: [1, 0, 0],   # 向前跳：执行后状态 [1, 0, 0]
    0x21010205: [1, 0, 0],   # 翻身：执行后状态 [1, 0, 0]
}

# 特技动作特殊处理（参考gesture_main.py的设计）
# 格式: {动作码: {"wait_after_state": 等待状态后的额外等待时间, "post_action": 状态确认后的收尾动作}}
ACTION_SPECIAL_HANDLING: Dict[int, Dict[str, Any]] = {
    0x2101030C: {  # 太空步：等待状态后sleep(4)，然后发送站立命令
        "wait_after_state": 4.0,
        "post_action": 0x21010202,  # 发送站立命令稳定
    },
}

# 语义消歧（移动/姿态）
POSTURE_CODE = {
    "posture_pitch": 0x21010130,
    "posture_roll": 0x21010131,
    "posture_yaw": 0x21010135,
}
MOVE_CODE = {
    "move_x": 0x21010130,
    "move_y": 0x21010131,
    "move_yaw": 0x21010135,
}


class RobotStatusWatcher:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Optional[List[int]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                st = status_listener()
                if st:
                    with self._lock:
                        self._latest = st
            except Exception as e:
                logging.error(f"状态监听线程异常: {e}")
                time.sleep(0.5)

    def stop(self) -> None:
        self._stop.set()

    def get_latest(self) -> Optional[List[int]]:
        with self._lock:
            return list(self._latest) if self._latest else None

    def wait_until(self, predicate, timeout: float, interval: float = 0.05) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.get_latest()
            if st is not None and predicate(st):
                return True
            time.sleep(interval)
        return False


class DogCommandExecutor:
    def __init__(self, dog_ip: str, dog_port: int = 43893):
        self.sfd, self.target_address = network_utils.setup_socket_and_address(dog_ip, dog_port)

        self._heartbeat_thread = MyRepeatThread("HeartbeatThread", self._safe_send_heartbeat, 0.25, None)
        self._heartbeat_thread.start()

        self._status = RobotStatusWatcher()
        self._cur_state: str = DogState.UNKNOWN

        self._perform_action(0x21010C02)  # 手动模式
        time.sleep(0.1)
        self._refresh_state(timeout=3.0)

    def close(self) -> None:
        try: self._status.stop()
        except: pass
        try:
            self._heartbeat_thread.stop()
            self._heartbeat_thread.join(timeout=1.0)
        except: pass
        try: self.sfd.close()
        except: pass

    def _safe_send_heartbeat(self) -> None:
        try:
            send_udp_heartbeat_once(self.sfd, self.target_address)
        except OSError:
            logging.warning("心跳发送失败 (socket可能已关闭), 停止心跳线程。")
            if self._heartbeat_thread:
                self._heartbeat_thread.stop()

    def _perform_action(self, code: int, param: int = 0, *_unused) -> None:
        perform_action(self.sfd, self.target_address, code, int(param), 0)

    def emergency_stop(self) -> None:
        self._perform_action(0x21020C0E, 0)

    def _send_stop_motion(self, duration: float = 1.5) -> None:
        """发送全轴停止指令，确保机器人完全停止"""
        logging.info(f"发送全轴停止指令，持续 {duration} 秒...")
        stop_time = time.time() + duration
        while time.time() < stop_time:
            self._perform_action(0x21010130, 0)  # x轴停止
            self._perform_action(0x21010131, 0)  # y轴停止
            self._perform_action(0x21010135, 0)  # yaw轴停止
            time.sleep(0.1)
        # 额外等待确保完全稳定（参考precise_control_main的1秒停顿）
        time.sleep(1.0)

    def _classify_state(self, st: List[int]) -> str:
        """分类机器人状态，参考gesture_main的状态识别逻辑"""
        if len(st) >= 1:
            basic_state = int(st[0])
            # 基本状态：6=站立，1=趴下（参考gesture_main）
            if basic_state == 6: return DogState.STANDING
            if basic_state == 1: return DogState.LYING
            # 特殊状态：20=打招呼动作状态，但机器人是站立的（参考gesture_main第183行）
            if basic_state == 20: return DogState.STANDING
            # 过渡状态：25和5是"打招呼"完成后的过渡状态，应该被识别为站立的过渡状态
            # 这些状态会自然过渡到[6, 0, 0]，不应该触发恢复流程
            if basic_state == 25: return DogState.STANDING
            if basic_state == 5: return DogState.STANDING
        return DogState.UNKNOWN

    def _refresh_state(self, timeout: float = 1.0) -> str:
        ok = self._status.wait_until(lambda s: len(s) >= 3, timeout=timeout)
        latest = self._status.get_latest() if ok else None
        self._cur_state = self._classify_state(latest) if latest else DogState.UNKNOWN
        logging.info(f"刷新状态完成, 当前: {self._cur_state}, 原始值: {latest}")
        return self._cur_state

    def _wait_motion_stable(self, timeout: float = 4.0) -> bool:
        """等待动作稳定，但如果超时则继续（避免阻塞）"""
        logging.info("等待动作稳定 (motion_state == 0)...")
        ok = self._status.wait_until(lambda s: len(s) >= 3 and int(s[2]) == 0, timeout=timeout)
        if not ok: 
            logging.warning("等待 motion_state==0 超时，继续执行（可能机器人仍在稳定中）")
            # 超时后等待一小段时间，然后继续
            time.sleep(1.0)
        return ok

    def _wait_for_state(self, target: str, timeout: float) -> bool:
        ok = self._status.wait_until(lambda s: self._classify_state(s) == target, timeout=timeout)
        if ok: self._cur_state = target
        return ok

    def _wait_for_execution_state(self, target_state: List[int], timeout: float = 10.0) -> bool:
        """等待动作进入执行状态（参考gesture_main.py的状态监听设计）"""
        logging.info(f"等待动作进入执行状态: {target_state}")
        ok = self._status.wait_until(
            lambda s: len(s) >= 3 and s[0] == target_state[0] and s[1] == target_state[1] and s[2] == target_state[2],
            timeout=timeout
        )
        if ok:
            logging.info(f"动作已进入执行状态: {target_state}")
        else:
            logging.warning(f"等待动作执行状态 {target_state} 超时")
        return ok

    def _wait_for_action_completion(self, execution_state: List[int], timeout: float = 15.0) -> bool:
        """等待动作执行完成（连续动作需要：等待状态离开执行状态并稳定）
        
        对于"打招呼"等动作，状态会从执行状态[20,0,0]过渡到[25,0,0]或[5,0,0]，
        最终稳定到[6,0,0]。我们需要等待稳定到[6,0,0]而不是立即尝试恢复。
        """
        logging.info(f"等待动作执行完成（离开执行状态 {execution_state}）...")
        deadline = time.time() + timeout
        in_execution = True
        
        # 先等待一小段时间，确保动作已经开始执行
        time.sleep(0.5)
        
        # 等待状态离开执行状态
        while time.time() < deadline:
            st = self._status.get_latest()
            if st is None or len(st) < 3:
                time.sleep(0.1)
                continue
            
            # 如果状态不再匹配执行状态，说明动作可能已完成
            if not (st[0] == execution_state[0] and st[1] == execution_state[1] and st[2] == execution_state[2]):
                if in_execution:
                    logging.info(f"动作状态已离开执行状态 {execution_state}，当前状态: {st}")
                    in_execution = False
                    
                    # 对于"打招呼"等动作，等待状态稳定到[6,0,0]（站立状态）
                    # 过渡状态[25,0,0]或[5,0,0]会被识别为STANDING，但我们需要等待稳定
                    if execution_state == [20, 0, 0]:  # 打招呼动作
                        logging.info("等待'打招呼'动作稳定到站立状态[6,0,0]...")
                        # 等待状态稳定到[6,0,0]，最多等待8秒
                        stable_deadline = time.time() + 8.0
                        while time.time() < stable_deadline:
                            st_check = self._status.get_latest()
                            if st_check and len(st_check) >= 3 and st_check[0] == 6 and st_check[2] == 0:
                                logging.info(f"动作已稳定到站立状态: {st_check}")
                                return True
                            time.sleep(0.1)
                        # 如果超时，但状态已经是STANDING（过渡状态），也认为完成
                        if self._classify_state(st) == DogState.STANDING:
                            logging.info("动作已进入站立过渡状态，继续执行")
                            return True
                    else:
                        # 其他动作：状态离开执行状态后，等待稳定
                        if self._wait_motion_stable(timeout=5.0):
                            logging.info("动作执行完成，状态已稳定")
                            return True
            
            time.sleep(0.1)
        
        # 如果超时，尝试等待稳定
        logging.warning(f"等待动作完成超时，尝试等待状态稳定...")
        self._wait_motion_stable(timeout=3.0)
        return False

    def _ensure_state(self, target: str, timeout: float = 8.0) -> None:
        """确保机器人处于目标状态，参考gesture_main的状态切换逻辑
        
        如果当前状态已经是目标状态（包括过渡状态），等待稳定即可，不需要切换。
        """
        self._refresh_state(timeout=1.0)
        if self._cur_state == target: 
            # 如果当前状态已经是目标状态，检查是否需要等待稳定
            latest = self._status.get_latest()
            if latest and len(latest) >= 3:
                # 如果状态是过渡状态（如25或5），等待稳定到最终状态（如6）
                if latest[0] in [25, 5] and target == DogState.STANDING:
                    logging.info(f"当前处于站立过渡状态 {latest}，等待稳定到[6,0,0]...")
                    stable_deadline = time.time() + 5.0
                    while time.time() < stable_deadline:
                        st_check = self._status.get_latest()
                        if st_check and len(st_check) >= 3 and st_check[0] == 6 and st_check[2] == 0:
                            logging.info(f"已稳定到站立状态: {st_check}")
                            self._cur_state = DogState.STANDING
                            return
                        time.sleep(0.1)
                    # 如果超时，但状态已经是STANDING，也认为成功
                    logging.info("过渡状态等待超时，但状态已识别为STANDING，继续执行")
                else:
                    # 即使状态匹配，也等待动作稳定
                    self._wait_motion_stable(timeout=2.0)
            return
        
        # 如果当前是UNKNOWN状态，先尝试恢复
        if self._cur_state == DogState.UNKNOWN:
            logging.info("当前状态未知，尝试发送零指令恢复...")
            try:
                self._perform_action(0x21010C05, 0)  # 零指令
                time.sleep(1.5)
                self._refresh_state(timeout=3.0)
                # 如果仍然是UNKNOWN，根据目标状态尝试切换
                if self._cur_state == DogState.UNKNOWN:
                    logging.warning(f"零指令后仍为UNKNOWN，直接尝试切换到目标状态: {target}")
                    self._perform_action(0x21010202, 0)  # 站立/趴下切换
                    time.sleep(1.0)
                    self._refresh_state(timeout=3.0)
            except Exception as e:
                logging.warning(f"UNKNOWN状态恢复过程异常: {e}，继续尝试切换...")
        
        self._wait_motion_stable(timeout=4.0)
        logging.info(f"当前: {self._cur_state}, 目标: {target}。尝试切换...")
        for attempt in range(2):
            try:
                self._perform_action(0x21010202, 0)  # 站立/趴下切换
            except Exception as e:
                logging.error(f"切换到目标状态 {target} 时发送指令异常: {e}")
                break

            time.sleep(0.5)  # 给状态切换一点时间
            if self._wait_for_state(target, timeout=timeout):
                logging.info(f"成功切换到: {self._cur_state}")
                # 切换成功后，等待动作稳定
                self._wait_motion_stable(timeout=3.0)
                return

            logging.warning(f"等待 {target} 超时 (第{attempt+1}次)，重试...")
            self._wait_motion_stable(timeout=2.0)

        # 到这里仍未成功，不再抛异常，避免整个任务进程崩溃，只记录错误并继续后续动作
        logging.error(f"无法可靠进入目标状态: {target}，后续动作将在当前状态 {self._cur_state} 下继续执行")

    def _run_repeat_action(self, code: int, seconds: float, val: int) -> None:
        th = MyRepeatThread(f"ACTION_{hex(code)}", self._perform_action, 0.1, seconds, code, val, 0)
        th.start()
        th.join()

    def _has_move(self, actions: List[Dict[str, Any]]) -> bool:
        return any(a.get("semantic") in MOVE_CODE for a in actions)

    def _prepare_for_first_move(self) -> None:
        """准备移动动作：确保站立状态并切换到移动模式"""
        logging.info("准备移动动作：切换到手动模式并确保站立...")
        self._perform_action(0x21010C02, 0)  # 手动模式
        time.sleep(0.3)
        self._ensure_state(DogState.STANDING, timeout=10.0)
        self._perform_action(0x21010D06, 0)  # 移动模式
        time.sleep(0.5)  # 给模式切换足够时间
        self._wait_motion_stable(timeout=3.0)

    def _exec_moonwalk(self) -> None:
        """太空步 0x2101030C 的专用执行逻辑，直接按 gesture_main.py 的实现移植。

        gesture_main.py 中的逻辑：
        1. 如果当前状态是 [6,0,0]（站立），则发送 ACTION_MOONWALK；
        2. 循环监听状态，当状态变为 [6,12,1] 时，认为太空步进入执行；
        3. sleep(4) 秒；
        4. 发送 0x21010202 收尾；
        5. 结束，不做额外状态修正，让下一个动作自己根据前置状态处理。
        """
        logging.info("执行太空步 0x2101030C（完全按 gesture_main 风格）...")

        try:
            # 1. 仅当当前是站立 [6,0,0] 时才执行太空步，否则直接跳过
            try:
                cur = status_listener()
            except Exception as e:
                logging.error(f"太空步前读取状态失败，放弃执行本次太空步: {e}")
                return

            if not (isinstance(cur, (list, tuple)) and len(cur) >= 3 and cur[0] == 6 and cur[1] == 0 and cur[2] == 0):
                logging.warning(f"当前状态不是站立[6,0,0]({cur})，不执行太空步，直接继续后续动作")
                return

            # 2. 发送太空步指令（等价于 gesture_main 里的 ACTION_MOONWALK）
            self._perform_action(0x2101030C, 0)

            # 3. 等待进入执行状态 [6,12,1]，超时则放弃，不做收尾
            logging.info("已发送太空步指令，等待状态变为[6,12,1]...")
            start = time.time()
            entered = False
            while time.time() - start < 8.0:
                st = status_listener()
                if isinstance(st, (list, tuple)) and len(st) >= 3 and st[0] == 6 and st[1] == 12 and st[2] == 1:
                    logging.info("检测到太空步执行状态 [6,12,1]，开始计时 4 秒（参考 gesture_main）...")
                    entered = True
                    break
                time.sleep(0.05)

            if not entered:
                logging.warning("在 8 秒内没有检测到太空步执行状态 [6,12,1]，放弃收尾，继续后续动作")
                return

            # 4. 进入执行状态后 sleep(4)，然后发送 0x21010202 收尾（与 gesture_main 一致）
            time.sleep(4.0)
            logging.info("太空步计时 4 秒结束，发送收尾动作 0x21010202（与 gesture_main 一致）...")
            self._perform_action(0x21010202, 0)

            # 5. 不做强制姿态修正，只简单记一次状态，交给后续动作自己处理
            self._refresh_state(timeout=1.0)
            logging.info(f"太空步收尾完成，当前状态: {self._cur_state}，后续动作将根据自身前置状态继续执行")

        except Exception as e:
            # 任何没预料到的异常都吞掉，只打日志，绝不让整个进程崩溃
            logging.error(f"太空步执行过程中出现未捕获异常，将跳过本动作继续后续动作: {e}")

    def _exec_motion(self, code: int, param: float, semantic: Optional[str]) -> None:
        """执行单个动作，参考gesture_main和precise_control_main的设计"""

        # 太空步单独走一条“仿 gesture_main”的专用流程，避免在通用逻辑里引入过多分支导致不稳定
        if code == 0x2101030C:
            self._exec_moonwalk()
            return

        need_state = PREREQUISITE_STATE.get(code)
        if need_state: 
            self._ensure_state(need_state)

        # 移动动作：参考precise_control_main的设计，每个动作后都有停顿
        if semantic in MOVE_CODE and MOVE_CODE[semantic] == code:
            # 确保在移动模式
            self._perform_action(0x21010D06, 0)
            time.sleep(0.2)
            
            if semantic == "move_x": 
                times, val = go_straight(param, 3)
            elif semantic == "move_y": 
                times, val = translate_left_and_right(param, 3)
            elif semantic == "move_yaw": 
                times, val = revolve_left_and_right(param)
            else: 
                raise ValueError("未知移动语义")
            
            # 执行移动动作
            self._run_repeat_action(code, times, val)
            # 完全停止（参考precise_control_main的1秒停顿）
            self._send_stop_motion()
            return

        # 姿态调整：必须在原地模式，且确保站立状态（抬头低头不能在趴下时执行）
        if semantic in POSTURE_CODE and POSTURE_CODE[semantic] == code:
            # 抬头低头需要站立状态
            if semantic == "posture_pitch":
                if self._cur_state != DogState.STANDING:
                    logging.warning(f"抬头低头需要站立状态，当前: {self._cur_state}，先切换到站立...")
                    self._ensure_state(DogState.STANDING, timeout=8.0)
            
            self._perform_action(0x21010D05, 0)  # 原地模式
            time.sleep(0.3)
            self._perform_action(code, int(param))
            time.sleep(0.8)  # 给姿态调整足够时间
            return

        # 特技动作：参考gesture_main.py的状态监听设计，但需要等待完成（连续动作）
        if code in ACTION_EXECUTION_STATE:
            execution_state = ACTION_EXECUTION_STATE[code]
            logging.info(f"执行特技动作 {hex(code)}，等待进入执行状态: {execution_state}")
            
            # 发送动作命令
            self._perform_action(code, 0)
            
            # 等待动作进入执行状态（参考gesture_main.py的while循环设计）
            if not self._wait_for_execution_state(execution_state, timeout=8.0):
                logging.warning(f"动作 {hex(code)} 未能在预期时间内进入执行状态，继续执行...")
            
            # 检查是否有特殊处理（如太空步需要sleep后发送站立命令）
            if code in ACTION_SPECIAL_HANDLING:
                handling = ACTION_SPECIAL_HANDLING[code]
                wait_time = handling.get("wait_after_state", 0)
                if wait_time > 0:
                    logging.info(f"动作 {hex(code)} 执行状态确认后，额外等待 {wait_time} 秒...")
                    time.sleep(wait_time)
                
                post_action = handling.get("post_action")
                if post_action:
                    logging.info(f"动作 {hex(code)} 执行完成，发送收尾动作: {hex(post_action)}")
                    self._perform_action(post_action, 0)
                    time.sleep(0.8)
                    # 刷新状态并等待稳定
                    self._refresh_state(timeout=2.0)
                    self._wait_motion_stable(timeout=3.0)
            
            # 对于站立类特技（除了有特殊处理的），等待动作执行完成
            elif PREREQUISITE_STATE.get(code) == DogState.STANDING:
                logging.info(f"站立类特技 {hex(code)} 执行状态确认，等待动作完成...")
                # 连续动作需要：等待状态离开执行状态并稳定
                self._wait_for_action_completion(execution_state, timeout=15.0)
                self._refresh_state(timeout=1.0)
                # 确保站立状态稳定
                self._wait_motion_stable(timeout=3.0)
            
            # 对于趴下类特技，执行状态就是完成状态[1,0,0]，等待稳定
            elif PREREQUISITE_STATE.get(code) == DogState.LYING:
                logging.info(f"趴下类特技 {hex(code)} 执行状态确认，等待稳定...")
                # 状态已经是[1,0,0]，直接等待稳定
                self._wait_motion_stable(timeout=6.0)
                self._refresh_state(timeout=1.0)
            
            logging.info(f"特技动作 {hex(code)} 执行完成，当前状态: {self._cur_state}")
            return

        # 其他动作
        self._perform_action(code, int(param))
        time.sleep(0.5)

    def exec_actions(self, payload: Dict[str, Any]) -> List[ExecResult]:
        actions = payload.get("actions", [])
        if not actions: raise ValueError("JSON中必须包含非空 actions 数组")

        has_move = self._has_move(actions)
        if has_move: self._prepare_for_first_move()

        results: List[ExecResult] = []
        for idx, act in enumerate(actions):
            started = time.time()
            code_raw, param, semantic = act.get("code"), act.get("param", 0), act.get("semantic")
            try:
                code = int(str(code_raw), 16)
            except Exception as e:
                results.append(ExecResult(False, idx, 0, param, f"code解析失败: {e}", started, time.time())); break

            if code == 0x21020C0E:
                try:
                    self.emergency_stop()
                    results.append(ExecResult(True, idx, code, param, "已急停", started, time.time()))
                except Exception as e:
                    results.append(ExecResult(False, idx, code, param, f"急停失败: {e}", started, time.time()))
                break

            try:
                logging.info(f"==> [动作 {idx+1}/{len(actions)}] 开始: {hex(code)}")
                self._exec_motion(code, float(param or 0), semantic)
                results.append(ExecResult(True, idx, code, param, "执行成功", started, time.time()))

                # 动作之间的准备：参考precise_control_main的1秒停顿
                is_last_action = idx == len(actions) - 1
                if not is_last_action:
                    try:
                        # 移动动作之间：已经通过_send_stop_motion处理了停顿
                        # 特技动作之间：需要准备下一个动作的状态
                        next_code = int(str(actions[idx + 1].get("code")), 16)
                        next_semantic = actions[idx + 1].get("semantic")
                        
                        # 如果下一个是移动动作，确保在移动模式
                        if next_semantic in MOVE_CODE:
                            self._perform_action(0x21010D06, 0)
                            time.sleep(0.3)
                        # 如果下一个是特技动作，准备状态
                        elif next_code in PREREQUISITE_STATE:
                            target_state = PREREQUISITE_STATE[next_code]
                            # 先刷新状态，避免基于错误状态做判断
                            self._refresh_state(timeout=1.0)
                            self._wait_motion_stable(timeout=3.0)  # 缩短超时时间，避免阻塞
                            logging.info(f"为下一个动作 {hex(next_code)} 准备，恢复到状态: {target_state}")
                            self._ensure_state(target_state, timeout=10.0)
                        # 其他情况：简单等待稳定
                        else:
                            self._wait_motion_stable(timeout=2.0)
                            time.sleep(0.5)  # 参考precise_control_main的停顿
                    except Exception as e:
                        logging.warning(f"动作间准备过程异常: {e}，继续执行下一个动作...")
                        time.sleep(0.5)  # 至少等待一小段时间

            except Exception as e:
                logging.error(f"[动作 {idx+1}] 异常: {e}")
                try: self.emergency_stop()
                except: pass
                results.append(ExecResult(False, idx, code, param, f"执行异常: {e}", started, time.time()))
                return results

        if not has_move:
            logging.info("动作序列结束，进入最终趴下（缓慢）")
            try:
                self._wait_motion_stable(timeout=6.0)
                self._ensure_state(DogState.LYING, timeout=12.0)
            except Exception as e:
                logging.warning(f"最终趴下失败/超时: {e}")
        else:
            self._send_stop_motion(duration=1.2)

        return results

def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dog-ip", default="192.168.1.120", help="UDP目标IP")
    p.add_argument("--dog-port", type=int, default=43893, help="UDP目标端口")
    p.add_argument("--json", required=True, help="动作序列JSON字符串")
    return p.parse_args(argv)

def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try: payload = json.loads(args.json)
    except Exception as e: print(json.dumps({"ok": False, "error": f"JSON解析失败: {e}"}, ensure_ascii=False)); return 2

    exec_ = DogCommandExecutor(args.dog_ip, args.dog_port)
    try:
        results = exec_.exec_actions(payload)
        out = {"ok": all(r.ok for r in results), "results": [r.__dict__ for r in results]}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["ok"] else 1
    finally:
        exec_.close()

if __name__ == "__main__":
    raise SystemExit(main())