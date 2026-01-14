#!/usr/bin/env python
# -*- coding: utf-8 -*-

import socket
import struct
import time

def send_udp_heartbeat_once(sfd, target_address, code=0x21040001, parameters_size=0, type_=0) -> None:
    """发送一次心跳包，不带循环和关闭socket。"""
    heartbeat_command = struct.pack('<III', code, parameters_size, type_)
    sfd.sendto(heartbeat_command, target_address)


def send_udp_heartbeat(sfd, target_address, code=0x21040001, parameters_size=0, type=0, heartbeat_interval=0.25) -> None:
    """原始的心跳函数，带循环，仅供参考，本项目不直接使用。"""
    heartbeat_command = struct.pack('<III', code, parameters_size, type)

    try:
        while True:
            start_time = time.time()
            sfd.sendto(heartbeat_command, target_address)
            time.sleep(max(0, heartbeat_interval - (time.time() - start_time)))
    except KeyboardInterrupt:
        print("Heartbeat sending stopped by user.")
    finally:
        sfd.close()
