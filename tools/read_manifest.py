# -*- coding: utf-8 -*-
"""安全读取 PE 内嵌 manifest（LOAD_LIBRARY_AS_DATAFILE，不执行代码）。
用法：python read_manifest.py <exe路径>"""
import ctypes
import sys

LOAD_LIBRARY_AS_DATAFILE = 0x00000002
RT_MANIFEST = 24

k32 = ctypes.windll.kernel32
k32.LoadLibraryExW.restype = ctypes.c_void_p
k32.FindResourceW.restype = ctypes.c_void_p
k32.LoadResource.restype = ctypes.c_void_p
k32.LockResource.restype = ctypes.c_void_p

path = sys.argv[1]
h = k32.LoadLibraryExW(path, None, LOAD_LIBRARY_AS_DATAFILE)
if not h:
    print("LoadLibraryEx 失败:", k32.GetLastError())
    sys.exit(1)

# 枚举 RT_MANIFEST 资源（可能有多个，ID 1 和 2）
found = False
for res_id in (1, 2, 3):
    hrsrc = k32.FindResourceW(ctypes.c_void_p(h), ctypes.c_wchar_p(res_id), ctypes.c_wchar_p(RT_MANIFEST))
    if not hrsrc:
        continue
    size = k32.SizeofResource(ctypes.c_void_p(h), ctypes.c_void_p(hrsrc))
    hres = k32.LoadResource(ctypes.c_void_p(h), ctypes.c_void_p(hrsrc))
    p = k32.LockResource(ctypes.c_void_p(hres))
    data = ctypes.string_at(p, size)
    print(f"=== manifest (res_id={res_id}, size={size}) ===")
    print(data.decode("utf-8", errors="replace"))
    found = True

if not found:
    print("未找到 RT_MANIFEST 资源（exe 无内嵌 manifest）")
