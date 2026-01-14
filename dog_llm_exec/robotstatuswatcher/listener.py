#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
# 这个相对导入会在 dog_llm_exec.py 中被替换为本地导入
# sys.path.append('../') 

from command.udp_command import *
from socketnetwork import network_utils

sock_fd = network_utils.set_up_recvfrom_socket_and_address()

def status_listener_radar(status_list, status_list_lock):
    while True:
        recv_data, _ = sock_fd.recvfrom(1024)
        recv_num = len(recv_data)
        if recv_num == 108:
            dr = JointStateReceived(recv_data)
            if dr.code == 2306:
                joint_angle = JointAngle(dr)
            if dr.code == 2307:
                joint_speed = JointSpeed(dr)
        elif recv_num == 212:
            dr, status_list_temp = RobotState(recv_data), []
            if dr.code == 2305:
                if dr.robot_basic_state != 0:
                    status_list_temp.append(dr.robot_basic_state)
                    status_list_temp.append(dr.robot_gait_state)
                    status_list_temp.append(dr.robot_motion_state)
                    status_list_temp.append(dr.distance_ahead)
                    with status_list_lock:
                        status_list[:] = status_list_temp

def status_listener():
    while True:
        recv_data, _ = sock_fd.recvfrom(1024)
        recv_num = len(recv_data)
        if recv_num == 108:
            dr = JointStateReceived(recv_data)
            if dr.code == 2306:
                joint_angle = JointAngle(dr)
            if dr.code == 2307:
                joint_speed = JointSpeed(dr)
        elif recv_num == 212:
            dr, status_list_temp = RobotState(recv_data), []
            if dr.code == 2305:
                if dr.robot_basic_state != 0:
                    status_list_temp.append(dr.robot_basic_state)
                    status_list_temp.append(dr.robot_gait_state)
                    status_list_temp.append(dr.robot_motion_state)
                    return status_list_temp
