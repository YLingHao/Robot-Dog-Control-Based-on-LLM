#!/usr/bin/env python
# -*- coding: utf-8 -*-

import socket
import struct
import ctypes
from ctypes import c_double, c_uint8, c_int32
# 这个 import 会在 dog_llm_exec.py 中被正确解析为本地导入
from command.udp_command import *

def send_command(sfd, target_address, code, parameters_size, type_) -> None:
    # 注意：在 Python 中，'type' 是预留关键字，这里我们使用 'type_'
    command_head = struct.pack('<3i', code, parameters_size, type_)
    # 发送命令头部到目标地址
    sfd.sendto(command_head, target_address)

def perform_action(sfd, target_address, code, parameters_size=0, type_=0) -> None:
    # 使用默认的 parameters_size 和 type 的值是 0
    send_command(sfd, target_address, code, parameters_size, type_)
