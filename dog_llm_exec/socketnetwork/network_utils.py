import socket
from typing import *

def setup_socket_and_address(dest_ip = '192.168.1.120', port=43893) -> Tuple[socket.socket, Tuple[str, int]]:
    # 创建UDP套接字
    sfd = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # 设置目标地址
    target_address = (dest_ip, port)
    # print(target_address)
    
    return sfd, target_address


def set_up_recvfrom_socket_and_address(ip_1='192.168.1.100', ip_2='192.168.1.101', port=43897) -> Optional[socket.socket] :
    """
    尝试绑定到主IP地址，若失败，则尝试备用IP地址。
    
    :param ip_1: 主IP地址
    :param ip_2: 备用IP地址
    :param port: 端口号
    :return: 绑定了IP地址和端口的UDP套接字，如果两个地址都失败则返回None。
    """
    # 创建UDP套接字
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # 尝试绑定到IP1地址
    try:
        target_address = (ip_1, port)
        sock.bind(target_address)
        print(f"成功连接到ip: {ip_1}, port: {port}")
        return sock
    except OSError as e:
        print(f"无法绑定到 ip: {ip_1}, error: {e}")

    # IP1地址失败，尝试备用IP2地址
    try:
        target_address = (ip_2, port)
        sock.bind(target_address)
        print(f"成功连接到ip: {ip_2}, port: {port}")
        return sock
    except OSError as e:
        print(f"无法绑定到 ip: {ip_2}, error: {e}")
        sock.close()  # 关闭套接字，释放资源
        return None
