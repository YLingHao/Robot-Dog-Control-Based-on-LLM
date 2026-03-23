#!/usr/bin/env python
# -*- coding: utf-8 -*-

import ctypes
import struct
import inspect

class CommandHead:
    def __init__(self, code=0, parameters_size=0, type_=0):
        self.code = code                                        # 指令码
        self.parameters_size = parameters_size                  # 指令值
        self.type_ = type_                                      # 指令类型

class Command:
    kDataSize = 256
    
    def __init__(self, head=None, data=None):
        if head is None:
            head = CommandHead()
        if data is None:
            data = [0] * self.kDataSize
        self.head = head
        self.data = data

class JointStateReceived(CommandHead):
    def __init__(self, data):
        """

        1.关节信息的头部信息为4字节命令字,4字节长度,4字节类型,后续为正文数据---(后续的所有都是类似)

        2. 1字节(b),2字节无符号短整型(H),4字节的无符号整型(I),接着是12个8字节的双精度浮点数(12d)

        3.code,parameters_size,type_的是继承于CommandHead头的,这样写是为了方便呈现出,数据都是基于CommandHead发送和接收的
        这样可以做到避免所有的复杂数据全部发在相同名字的Command中,而是重新定义一个基于CommandHead的结构体

        4.“self.code, ” 逗号是为了去除掉由  struct.unpack  操作产生的元组问题，可以去去掉逗号直接复制

        """
        self.data = data        # 关节的状态数据，全部接受，再由code来决定分配给谁
        CommandHead.code, = struct.unpack('<I', self.data[:4])


class JointAngle(CommandHead):
    def __init__(self, joint_state_received):
        # 特定于关节角度的数据解析
        *self.joint_angles, = struct.unpack('<12d', joint_state_received.data[12:])

class JointSpeed(CommandHead):
    def __init__(self, joint_state_received):
        # 特定于关节速度的数据解析
        *self.joint_speeds, = struct.unpack('<12d', joint_state_received.data[12:])

class RobotState(CommandHead):
    def __init__(self, data):
        
        CommandHead.code, = struct.unpack('<I', data[:4])
        CommandHead.parameters_size, = struct.unpack('<I', data[4:8])
        CommandHead.type_, = struct.unpack('<I', data[8:12])
        self.robot_basic_state, = struct.unpack('<I', data[12:16])                 # 机器人基本运动状态
        self.robot_gait_state, = struct.unpack('<I', data[16:20])                  # 机器人步态信息
        self.robot_motion_state, = struct.unpack('<i', data[176:180])              # 机器人动作状态
        self.distance_ahead, = struct.unpack('<d', data[-16:-8])                   # 雷达前方的距离
        self.rear_distance, = struct.unpack('<d', data[-8:])
        

        # self.rpy = struct.unpack('<3d', data[20:44])                               # IMU角度
        # self.rpy_vel = struct.unpack('<3d', data[44:68])                           # IMU角速度
        # self.xyz_acc = struct.unpack('<3d', data[68:92])                           # IMU加速度
        # self.pos_world = struct.unpack('<3d', data[92:116])                        # 机器人在世界坐标系中的位置
        # self.vel_world = struct.unpack('<3d', data[116:140])                       # 机器人在世界坐标系中的速度
        # self.vel_body = struct.unpack('<3d', data[140:164])                        # 机器人在体坐标系中的速度
        # self.touch_down_and_stair_trot = struct.unpack('<I', data[164:168])        # 此功能暂时未激活。此数据仅用于占位
        # self.is_charging = struct.unpack('<b', data[168:169])                      # 暂时未开放
        # self.error_state = struct.unpack('<I', data[169:173])                      # 暂时未开放


dog_actions = {
    # 简单指令
    "ACTION_STAND_DOWN": 0x21010202,                    # 起立/趴下 0x21010202 - 0 在趴下状态和初始站立状态之间轮流切换
    "ACTION_ZERO": 0x21010C05,                          # 回零 0x21010C05 - 0 初始化机器人关节
    "ACTION_EMERGENCY_STOP": 0x21020C0E,                # 软急停 0x21020C0E - 0 使机器人软急停
    "ACTION_FLATLAND_SLOW_WALK": 0x21010300,            # 平地低速步态 0x21010300 - 0 使机器人从当前步态切换到低速步态
    "ACTION_FLATLAND_MEDIUM_WALK": 0x21010307,          # 平地中速步态 0x21010307 - 0 使机器人从当前步态切换到中速步态
    "ACTION_FLATLAND_FAST_WALK": 0x21010303,            # 平地高速步态 0x21010303 - 0 使机器人从当前步态切换到高速步态
    "ACTION_NORMAL_CRAWL": 0x21010406,                  # 正常/匍匐 0x21010406 - 0 使机器人从当前步态切换到匍匐低速行走步态，或从匍匐低速行走步态切至正常低速行走步态
    "ACTION_GRASPING_OBSTACLE_WALK": 0x21010402,        # 抓地越障步态 0x21010402 - 0 使机器人从当前步态切换到抓地步态
    "ACTION_GENERAL_OBSTACLE_WALK": 0x21010401,         # 通用越障步态 0x21010401 - 0 使机器人从当前步态切换到通用步态
    "ACTION_HIGH_STEP_OBSTACLE_WALK": 0x21010407,       # 高踏步越障步态 0x21010407 - 0 使机器人从当前步态切换到高踏步步态
    "ACTION_TWIST_BODY": 0x21010204,                    # 扭身体 0x21010204 - 0 处于力控状态（静止站立）
    "ACTION_ROLL_OVER": 0x21010205,                     # 翻身 0x21010205 - 0 趴下状态
    "ACTION_MOONWALK": 0x2101030C,                      # 太空步 0x2101030C - 0 处于力控状态（静止站立）
    "ACTION_BACKFLIP": 0x21010502,                      # 后空翻 0x21010502 - 0 趴下状态
    "ACTION_GREET": 0x21010507,                         # 打招呼 0x21010507 - 0 趴下状态
    "ACTION_JUMP_FORWARD": 0x2101050B,                  # 向前跳 0x2101050B - 0 趴下状态
    "ACTION_TWIST_JUMP": 0x2101020D,                    # 扭身跳 0x2101020D - 0 处于力控状态（静止站立）

    # 原地模式
    "ACTION_In_place_mode": 0x21010D05,                 # 原地模式: 0x21010D05 - 0
    "ACTION_Adjust_the_roll_angle": 0x21010131,         # 调整横滚角: 0x21010131 0 [-12553,12553]，取正值时向右翻滚
    "ACTION_Adjust_the_pitch_angle": 0x21010130,        # 调整俯仰角: 0x21010130 0 [-6553,6553]，取正值时低头
    "ACTION_Adjust_the_height_of_body": 0x21010102,     # 调整身体高度: 0x21010102 0 [-20000,20000]，取正值时抬高身体
    "ACTION_Adjust_the_yaw_angle": 0x21010135,          # 调整偏航角: 0x21010135 0 [-9553,9553]，取正值时向右旋转
    
    # 移动模式
    "ACTION_Mobile_mode": 0x21010D06,                   # 移动模式: 0x21010D06 - 0
    "ACTION_Translate_left_and_right": 0x21010131,      # 左右平移: 0x21010131 0 [-12553,12553]，指定机器人 y 轴上的期望线速度，正值向右
    "ACTION_pan_back_and_forth": 0x21010130,            # 前后平移: 0x21010130 0 [-6553,6553]，指定机器人 x 轴上的期望线速度，正值向前
    "ACTION_turn_left_and_right": 0x21010135,           # 左右转弯: 0x21010135 0 [-9553,9553]，指定机器人的期望角速度，正值向右转

    # 自主模式 
    "Autonomous_mode": 0x21010C03,                      # 自主模式 0x21010C03 - 0 使机器人从手动模式切入自主模式

    # 手动模式 
    "Manual_mode": 0x21010C02,                          # 手动模式 0x21010C02 - 0 使机器人从自主模式切入手动模式   

    # 超声波雷达
    "radar": 0x21012109                                 # 0x21012109 0x40 0 超声波有效量程为[0.28m,4.50m]，
                                                        # 当障碍物距离小于 0.28m 时显示为 0.28m，
                                                        # 当障碍物距离大于 4.50m 时显示为 4.50m。
}




