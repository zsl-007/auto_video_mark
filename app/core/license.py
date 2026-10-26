"""
-------------------------------------------------
Project Name: license
File Name: license.py
Author: Senlin Zhao
Create Date: 2025/3/17 10:56
Update Date: 2025/3/17 10:56
Description: 授权验证，如果有授权文件，验证授权文件。如果没有授权文件或授权文件错误，则返回机器码

pip install cryptocode  -i https://mirrors.aliyun.com/pypi/simple/
-------------------------------------------------
"""

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as crypto_padding
import uuid
import socket
import hashlib
import os
from datetime import datetime
import cryptocode

def license_auth():
    result_response = {
        "code": -101,
        "result": {
            "message": "auth error!"
        }
    }
    
    # 授权成功  # 暂时解除授权
    #result_response['code'] = 1
    #result_response['result']['message'] = 'License verified successfully.'
    #return result_response

    machineCodeStr = generate_machine_code()

    # 推荐：使用绝对路径 ##docker部署时，使用挂载卷路径
    LICENSE_PATH = "/app/license/"
    LICENSE_PATH_FALLBACK = os.path.join(os.path.dirname(__file__), '..', 'license')

    #privateKeyPemFilePath = os.path.join(os.path.dirname(__file__), 'private_key.pem') # 相对路径，本函数所在路径
    privateKeyPemFilePath = os.path.join(LICENSE_PATH, 'private_key.pem') ##docker部署时，使用挂载卷路径
    if os.path.exists(privateKeyPemFilePath) == False:
        fallback_private_key = os.path.join(LICENSE_PATH_FALLBACK, 'private_key.pem')
        if os.path.exists(fallback_private_key) == False:
            result_response['result']['message'] = f'private_key.pem not found. machineCode:{machineCodeStr}'
            return result_response
        privateKeyPemFilePath = fallback_private_key

    #licenseFilePath = os.path.join(os.path.dirname(__file__), 'license.lic') # 相对路径，本函数所在路径
    licenseFilePath = os.path.join(LICENSE_PATH, 'license.lic') ##docker部署时，使用挂载卷路径
    if os.path.exists(licenseFilePath) == False:
        fallback_lic_key = os.path.join(LICENSE_PATH_FALLBACK, 'license.lic')
        if os.path.exists(fallback_lic_key) == False:
            result_response['result']['message'] = f'license.lic not found. machineCode:{machineCodeStr}'
            return result_response
        licenseFilePath = fallback_lic_key

    try:
        # 读取私钥文件
        with open(privateKeyPemFilePath, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

        # 读取license文件
        with open(licenseFilePath, "rb") as f:
            license_data = f.read()

        # 解密数据
        decrypted = private_key.decrypt(
            license_data,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        #print("Decrypted:", decrypted)

        # 解析解密后的数据
        decrypted_str = decrypted.decode('utf-8').strip()

        # 使用自定义密钥解密
        miyao = 'smile'
        mingwen = cryptocode.decrypt(decrypted_str, miyao)
        decrypted_str = mingwen

        lines = decrypted_str.split('\n')
        #print(len(lines))
        if len(lines) < 2:
            result_response['result']['message'] = f'Invalid license format. machineCode:{machineCodeStr}'
            return result_response

        license_machine_code = lines[0]
        license_expire_date = lines[1]

        # 验证机器码
        if license_machine_code != machineCodeStr:
            result_response['result']['message'] = f'Machine code mismatch. machineCode:{machineCodeStr}'
            return result_response

        # 验证日期
        if not is_valid_date(license_expire_date):
            result_response['result']['message'] = f'Invalid date format. machineCode:{machineCodeStr}'
            return result_response

        if datetime.strptime(license_expire_date, "%Y-%m-%d") < datetime.now():
            result_response['result']['message'] = f'License expired. machineCode:{machineCodeStr}'
            return result_response

        # 授权成功
        result_response['code'] = 1
        result_response['result']['message'] = 'License verified successfully.'
        return result_response

    except Exception as e:
        result_response['result']['message'] = f'Exception: {e}. machineCode:{machineCodeStr}'
        return result_response

def is_valid_date(date_str, date_format="%Y-%m-%d"):
    """
    校验日期是否有效
    :param date_str: 日期字符串
    :param date_format: 日期格式，默认为"YYYY-MM-DD"
    :return: 如果日期有效返回True，否则返回False
    """
    try:
        datetime.strptime(date_str, date_format)
        return True
    except ValueError:
        return False
def get_mac_address():
    """
    返回当前设备的MAC地址。

    该函数通过uuid库获取当前设备的UUID，然后从中提取出MAC地址部分。
    MAC地址是网络设备的唯一标识，通常用于网络通信。

    Returns:
        str: 当前设备的MAC地址，格式为XX:XX:XX:XX:XX:XX。
    """
    # 使用uuid库获取设备的唯一标识符
    mac = uuid.UUID(int = uuid.getnode()).hex[-12:]
    # 将MAC地址格式化为XX:XX:XX:XX:XX:XX的形式
    return ":".join([mac[e:e+2] for e in range(0,11,2)])

def get_ip_address():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return ""

def generate_machine_code():
    mac = get_mac_address()
    ip = get_ip_address()
    return hashlib.sha256((mac + ip).encode()).hexdigest()
