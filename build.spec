# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# 怪物模板由使用者按 F8 现场生成，没有时不能让整个打包失败
datas = [
    ('config.json', '.'),  # 包含配置文件
    ('summon_button_custom.png', '.'),  # 召唤Boss按钮模板，缺失会无法识别面板
]
if os.path.isfile('monster_template_custom.png'):
    datas.append(('monster_template_custom.png', '.'))
# 样本模板随包内置；程序目录存在 templates 时优先用外部的
if os.path.isdir('templates'):
    datas.append(('templates', 'templates'))

a = Analysis(
    ['release.py'],  # 替换为你的主脚本文件名
    pathex=[], 
    binaries=[],
    datas=datas,
    hiddenimports=[
        'pynput.keyboard._win32',  # Windows 系统需要
        'pynput.mouse._win32',
        'pyautogui',
        'cv2',
        'numpy'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Cf_Release',  # 生成的exe名称
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # 使用UPX压缩
    runtime_tmpdir=None,
    console=False,  # 不显示控制台
    icon='CrossFire.ico',  # 可选图标文件
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True  # 请求管理员权限
)