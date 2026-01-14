#!/usr/bin/env python
# -*- coding: utf-8 -*-

from command.udp_command import *                       # 存放各种结构体与状态数据、指令

def go_straight(long, speedgear=3):
    # 档位速度只是参考值，具体速度按照实际来判断，默认速度是3档
    speedgear_ditc = {
        1:7000,
        2:7500,
        3:8000,
        4:8700,
        5:9000,
        6:10500,
    }

    val = speedgear_ditc[speedgear]

    if long < 0:
        val = -val
        speed_per_second_meter = (-1.5059e-08) * val ** 2 - (4.1944e-04) * val - 2.1804
        times = abs(long / speed_per_second_meter)
    else:
        speed_per_second_meter = (-7.237e-09) * val ** 2 + 0.0002933 * val - 1.643
        times = long / speed_per_second_meter
    return [times, val]


def translate_left_and_right(long, speedgear=3):
    # 档位速度只是参考值，具体速度按照实际来判断，默认速度是3档
    speedgear_ditc = {
        1: 14000,
        2: 18000,
        3: 21000,
        4: 24000,
        5: 27000,
        6: 34000,
    }
    val = speedgear_ditc[speedgear]
    if long < 0:
        val = -val
        speed_per_second_meter = -(1.6e-05) * val - 0.2256
        times = abs(long / speed_per_second_meter)
    else:
        speed_per_second_meter = (1.517e-05) * val  - 0.1748
        times = long / speed_per_second_meter
    return [times, val]


def revolve_left_and_right(angle):
    def find_closest_output(input_value):
        # 判断角度是否超过了360度，超过了则需要则算掉
        if input_value > 360:
            input_value = input_value / (input_value % 360)

        # 输出到输入的映射字典
        output_to_input_map = {
            0: 0,
            15: 0.1,
            30: 0.2,
            45: 0.53,
            60: 0.6,
            75: 0.9,
            90: 1.3,
            105: 1.5,
            120: 1.9,
            135: 2.2,
            150: 2.3,
            165: 2.5,
            167: 2.8,
            185: 2.9,
            195: 3.1,
            210: 3.4,
            225: 3.7,
            240: 4,
            255: 4.3,
            270: 4.5,
            285: 4.7,
            300: 5,
            315: 5.3,
            330: 5.6,
            345: 5.8,
            360: 5.9,
        }
        # 获取所有输出键并将其转换为列表
        keys = list(output_to_input_map.keys())
        # 寻找与输入值最接近的输出键
        closest_key = min(keys, key=lambda x: abs(x - input_value))
        # 返回与最接近键关联的输入值
        return output_to_input_map[closest_key]
    

    times = find_closest_output(abs(angle))  # 获取时间与角度的关系
    if angle < 0:
        val = -10000
    else:
        val = 10000

    return [times, val]



