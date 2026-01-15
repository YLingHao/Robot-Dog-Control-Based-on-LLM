#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""避障功能模块

集成 5_obstacle_avoidance 项目的避障功能：
- 雷达检测障碍物（距离 < 0.4m）
- 图像识别检测楼梯和坑洞
- 检测到障碍物时执行避障序列
- 检测到楼梯/坑洞时暂停机器狗
"""

import logging
import math
import threading
import time
from typing import List, Optional, Tuple

from command.udp_command import RobotState
from sendcommand.SendToCommand import perform_action
from speeds.sportspeed import go_straight, translate_left_and_right, revolve_left_and_right
from robotstatuswatcher.listener import status_listener

# 尝试导入雷达监听函数
try:
    from robotstatuswatcher.listener import status_listener_radar
    RADAR_LISTENER_AVAILABLE = True
except ImportError:
    RADAR_LISTENER_AVAILABLE = False
    logging.warning("雷达监听函数不可用，将使用基本状态监听")

# 注意：不在这里调用 logging.basicConfig()，因为主进程已经配置了日志系统
# 这样可以避免日志重复输出


class ObstacleAvoidanceManager:
    """避障管理器：负责检测障碍物、楼梯、坑洞，并执行避障动作"""
    
    def __init__(self, sfd, target_address, enable_radar: bool = True, enable_camera: bool = True):
        """
        初始化避障管理器
        
        Args:
            sfd: UDP socket 文件描述符
            target_address: 目标地址
            enable_radar: 是否启用雷达检测
            enable_camera: 是否启用摄像头检测（楼梯/坑洞）
        """
        self.sfd = sfd
        self.target_address = target_address
        self.enable_radar = enable_radar
        self.enable_camera = enable_camera
        
        # 避障距离限制（米）
        self.obs_void_distance = 0.6
        
        # 避障序列参数
        self.avoid_actions_params = {
            1: [(0.9,), (0.9,)],  # go_straight: 前进0.9米两次
            3: [(-46.0,), (91.0,), (-30.0,)]  # revolve_left_and_right: 左转-46度，右转91度，左转-30度
        }
        self.avoid_actions_sequence = [(3, 0), (1, 0), (3, 1), (1, 1), (3, 2)]  # 避障动作序列
        
        # 检测结果（线程安全）
        self._radar_status_list: List[float] = []  # 雷达状态列表 [x, y, z, distance, ...]
        self._radar_lock = threading.Lock()
        self._camera_result: List[str] = []  # 摄像头检测结果 ['staircase'] 或 ['hole']
        self._camera_lock = threading.Lock()
        
        # 检测线程
        self._radar_thread: Optional[threading.Thread] = None
        self._camera_thread: Optional[threading.Thread] = None
        self._running = False
        
        # 计数器（避免重复触发）
        self.obstacle_count = 0
        self.staircase_count = 0
        self.hole_count = 0
        
    def start(self):
        """启动避障检测"""
        if self._running:
            return
        
        self._running = True
        
        # 启动雷达检测
        if self.enable_radar:
            # 开启雷达
            perform_action(self.sfd, self.target_address, 0x21012109, 0x40, 0)
            time.sleep(0.5)
            self._radar_thread = threading.Thread(target=self._radar_detection_loop, daemon=True)
            self._radar_thread.start()
            logging.info("避障管理器：雷达检测已启动")
        
        # 启动摄像头检测
        if self.enable_camera:
            try:
                # 延迟导入，避免在没有摄像头时出错
                from obstacle_model_cap import inference_loop
                self._camera_thread = threading.Thread(
                    target=inference_loop,
                    args=(self._camera_result, self._camera_lock),
                    daemon=True
                )
                self._camera_thread.start()
                logging.info("避障管理器：摄像头检测已启动")
            except ImportError as e:
                logging.warning(f"避障管理器：无法启动摄像头检测（缺少依赖）: {e}")
                self.enable_camera = False
        
    def stop(self):
        """停止避障检测"""
        self._running = False
        if self._radar_thread:
            self._radar_thread.join(timeout=1.0)
        if self._camera_thread:
            self._camera_thread.join(timeout=1.0)
        logging.info("避障管理器：已停止")
    
    def _radar_detection_loop(self):
        """雷达检测循环"""
        # 使用线程安全的雷达状态列表
        radar_status_list = []
        
        if RADAR_LISTENER_AVAILABLE:
            # 使用专门的雷达监听函数（会更新 radar_status_list）
            def radar_listener_wrapper():
                status_listener_radar(radar_status_list, self._radar_lock)
            
            radar_listener_thread = threading.Thread(target=radar_listener_wrapper, daemon=True)
            radar_listener_thread.start()
        
        while self._running:
            try:
                if RADAR_LISTENER_AVAILABLE:
                    # 从雷达监听线程更新的列表中获取距离信息
                    with self._radar_lock:
                        if radar_status_list and len(radar_status_list) >= 4:
                            self._radar_status_list = radar_status_list[:4]  # [basic_state, gait_state, motion_state, distance]
                else:
                    # 降级方案：使用基本状态监听（不包含距离信息）
                    status = status_listener()
                    if status and len(status) >= 3:
                        with self._radar_lock:
                            # 基本状态监听不包含距离，设置为无穷大（不会触发避障）
                            self._radar_status_list = status[:3] + [float('inf')]
            except Exception as e:
                logging.error(f"雷达检测异常: {e}")
            time.sleep(0.1)  # 100ms检测一次
    
    def check_obstacle(self) -> bool:
        """检查是否有障碍物（雷达检测）"""
        if not self.enable_radar:
            return False
        
        with self._radar_lock:
            if self._radar_status_list and len(self._radar_status_list) >= 4:
                distance = self._radar_status_list[3] if len(self._radar_status_list) > 3 else float('inf')
                if distance <= self.obs_void_distance:
                    return True
        return False
    
    def check_staircase(self) -> bool:
        """检查是否检测到楼梯"""
        if not self.enable_camera:
            return False
        
        with self._camera_lock:
            return self._camera_result == ['staircase']
    
    def check_hole(self) -> bool:
        """检查是否检测到坑洞"""
        if not self.enable_camera:
            return False
        
        with self._camera_lock:
            return self._camera_result == ['hole']
    
    def execute_avoid_sequence(self):
        """执行避障序列"""
        logging.info("执行避障序列...")
        
        from threading_utils.ThreadTemplates import MyRepeatThread
        from sendcommand.SendToCommand import perform_action
        
        # 动作包装函数（参考 ThreadTemplates.py）
        def action_go_straight(long, speedgear=3):
            times, val = go_straight(long, speedgear)
            thread = MyRepeatThread(
                "ACTION_go_straight",
                perform_action,
                0.1, times,
                self.sfd, self.target_address, 0x21010130, val, 0
            )
            return thread
        
        def action_revolve_left_and_right(angle):
            times, val = revolve_left_and_right(angle)
            thread = MyRepeatThread(
                "ACTION_revolve_left_and_right",
                perform_action,
                0.1, times,
                self.sfd, self.target_address, 0x21010135, val, 0
            )
            return thread
        
        # 动作字典映射
        actions_dict = {
            1: action_go_straight,
            3: action_revolve_left_and_right
        }
        
        # 遍历避障动作序列
        for idx, (action_id, params_index) in enumerate(self.avoid_actions_sequence):
            try:
                action_func = actions_dict.get(action_id, None)
                if action_func:
                    avoid_params = self.avoid_actions_params[action_id][params_index]
                    if isinstance(avoid_params, tuple):
                        avoid_thread = action_func(*avoid_params)
                        avoid_thread.start()
                        avoid_thread.join()
                        # 每个动作后等待稳定
                        if idx < len(self.avoid_actions_sequence) - 1:  # 最后一个动作不需要额外等待
                            time.sleep(0.8)  # 等待动作稳定
            except Exception as e:
                logging.error(f"避障序列第 {idx+1} 个动作执行失败: {e}")
                # 如果某个动作失败，发送停止指令
                perform_action(self.sfd, self.target_address, 0x21010407, 0, 0)
                raise
        
        logging.info("避障序列执行完成")
    
    def handle_obstacle(self, current_thread, params: Tuple, before_long: float = 0.0) -> bool:
        """
        处理障碍物
        
        Args:
            current_thread: 当前执行的动作线程
            params: 当前动作的参数
            before_long: 已经前进的距离（如果为0，会从线程计算）
            
        Returns:
            bool: 是否检测到障碍物并执行了避障
        """
        if self.obstacle_count > 0:
            return False
        
        if self.check_obstacle():
            self.obstacle_count += 1
            logging.warning(f"检测到障碍物（距离 <= {self.obs_void_distance}m），执行避障...")
            
            # 计算已前进的距离（如果未提供）
            if before_long == 0.0 and current_thread and hasattr(current_thread, 'print_attributes'):
                try:
                    attrs = current_thread.print_attributes()
                    current_time_start = attrs.get('current_time_start', 0)
                    # 使用 go_straight 的特殊模式计算已前进距离
                    before_long = go_straight(
                        long=9999,
                        times=current_time_start,
                        obs_void_distance=self.obs_void_distance
                    )
                except Exception as e:
                    logging.warning(f"计算已前进距离失败: {e}，使用默认值0")
                    before_long = 0.0
            
            # 停止当前动作（先停止，不要急停，急停会导致机器狗趴下）
            if current_thread and hasattr(current_thread, 'stop'):
                current_thread.stop()
                if hasattr(current_thread, 'join'):
                    current_thread.join(timeout=1.0)  # 等待线程结束，最多1秒
            
            # 等待动作稳定（不发送急停指令，避免机器狗趴下）
            time.sleep(0.5)
            
            # 执行避障序列
            try:
                self.execute_avoid_sequence()
            except Exception as e:
                logging.error(f"执行避障序列失败: {e}")
                # 如果避障序列执行失败，只发送停止指令，不发送急停
                perform_action(self.sfd, self.target_address, 0x21010407, 0, 0)  # 站立停止
            
            # 更新路线参数（如果需要）
            # 这里简化处理，实际应该根据已前进的距离更新剩余路径
            temp = 2 * self.avoid_actions_params[1][0][0] / math.sqrt(2)
            logging.info(f"避障完成，已前进距离: {before_long:.3f}m, 避障消耗距离: {temp:.3f}m")
            
            return True
        return False
    
    def handle_staircase(self) -> bool:
        """处理楼梯检测"""
        if self.staircase_count > 0:
            return False
        
        if self.check_staircase():
            self.staircase_count += 1
            logging.warning("检测到楼梯，暂停机器狗...")
            perform_action(self.sfd, self.target_address, 0x21010407, 0, 0)
            return True
        return False
    
    def handle_hole(self) -> bool:
        """处理坑洞检测"""
        if self.hole_count > 0:
            return False
        
        if self.check_hole():
            self.hole_count += 1
            logging.warning("检测到坑洞，暂停机器狗...")
            perform_action(self.sfd, self.target_address, 0x21010406, 0, 0)
            return True
        return False
    
    def reset_counters(self):
        """重置计数器（用于新的动作序列）"""
        self.obstacle_count = 0
        self.staircase_count = 0
        self.hole_count = 0
