# -*- coding: utf-8 -*-
"""常驻冒烟工具 tools/smoke_exe.py 的判定逻辑（R236）。

冒烟工具本身跑的是打包出来的 exe，不能进单元门禁（会真的弹窗、要几十秒）；但它的**判据**
必须是确定性纯函数——R235 的现场教训是「出现 2 个进程即算通过」把 77MB 半加载态判成绿，
判错的是函数逻辑而不是运气。这里把这些错判（无标题 / 枚举自身故障 / 半加载 / 未稳态 /
残留 / 污染配置）和一个真绿样本钉死，另外用 AST 守住「不经过 shell 拼命令」这条安全边界。
"""
import ast
import pathlib
import sys
import types

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
sys.modules.setdefault("_console", types.ModuleType("_console"))   # 别在 pytest 进程里改 stdout
sys.path.insert(0, str(TOOLS))
import smoke_exe as sx  # noqa: E402

READY = [262.0, 266.0, 268.0, 268.0, 268.0]     # 实测就绪形态（6s 出标题、稳态 268MB）


def test_tasklist_csv_parsed_without_third_party():
    out = ('"AgentHub.exe","4212","Console","1","77,000 K"\r\n'
           '"AgentHub.exe","3980","Console","1","268,000 K"\r\n'
           '"Notepad.exe","111","Console","1","1,000 K"\r\n'
           '"AgentHub.exe","abc","Console","1","1 K"\r\n')
    assert sx.parse_tasklist(out, "AgentHub.exe") == [3980, 4212]
    assert sx.parse_tasklist("", "AgentHub.exe") == []
    assert sx.parse_tasklist(out, "agenthub.exe") == [3980, 4212]   # 大小写不敏感


def test_no_window_title_is_not_a_pass():
    """R235 假绿的根形态：进程有了、标题没出现 → 必须判失败。"""
    v = sx.verdict("", 12.0, 0.0, [], [], True)
    assert not v["ok"] and "标题" in v["reasons"][0]


def test_half_loaded_77mb_is_not_a_pass():
    """标题有了但内存 77MB 且已「稳」在 77MB——旧口径正是这样绿的，本轮判它不合格。"""
    flat = [77.0] * 5
    v = sx.verdict("AgentHub", 6.0, flat[-1], flat, [], True)
    assert not v["ok"] and any("半加载" in r for r in v["reasons"])


def test_ready_window_passes():
    v = sx.verdict("AgentHub", 6.0, READY[-1], READY, [], True)
    assert v["ok"] and v["reasons"] == []
    assert v["steady_mb"] == 268.0 and v["elapsed_s"] == 6.0


def test_is_steady_needs_enough_samples_and_a_real_platform():
    assert not sx.is_steady([])
    assert not sx.is_steady([268.0, 268.0, 268.0])                 # 样本不足（需 ≥4）
    assert not sx.is_steady([100.0, 140.0, 180.0, 220.0])          # 持续增长不是平台
    assert not sx.is_steady([0.0, 0.0, 0.0, 0.0])                  # 取不到内存不算稳
    assert sx.is_steady(READY)
    assert not sx.is_steady(READY, tol=0.001)                      # 容差可注入，默认值写在常量


def test_residue_and_config_tampering_are_each_fatal():
    assert not sx.verdict("AgentHub", 6.0, READY[-1], READY, [3980], True)["ok"]
    bad = sx.verdict("AgentHub", 6.0, READY[-1], READY, [], False)
    assert not bad["ok"] and any("config.json" in r for r in bad["reasons"])


def test_broken_enumeration_is_not_reported_as_dead_window():
    """R236 实锤：工具找错 DLL（GetWindowThreadProcessId 在 user32 不在 kernel32）时，
    回调每次都抛异常、ctypes 吞掉并继续枚举，现场只看到「一个窗口也没找到」——
    判据①于是把工具故障报成产品故障。枚举异常必须单独成条、排在最前。"""
    v = sx.verdict("", 60.0, 0.0, [], [], True,
                   enum=(37, "AttributeError: function 'GetWindowThreadProcessId' not found"))
    assert not v["ok"]
    assert "枚举异常" in v["reasons"][0] and "37" in v["reasons"][0]
    assert any("标题" in r for r in v["reasons"][1:])


def test_real_window_enumeration_raises_nothing():
    """在真桌面枚举一次：任何回调异常都会让 enum_errors 计数非零（即上面那条 k32 坑）。

    这是「工具自身可用」的守卫，不是判据守卫——它红了说明冒烟结论不可信。
    """
    sx._ENUM_FAILS.update(count=0, first="")
    assert sx.find_window("绝无此标题_zzz_9k", {1}) is None
    assert sx.enum_errors() == (0, ""), sx.enum_errors()


def test_module_launches_without_shell_and_reads_steady_constants():
    """AST 守卫：不出现 shell=True；subprocess.* 的命令必须是列表形态（防注入 + 防 cmd 解析差异）。

    只看 `subprocess.xxx(...)` 的调用——本模块自己也有个纯函数叫 run()，按裸函数名匹配会误伤。
    """
    tree = ast.parse((TOOLS / "smoke_exe.py").read_text(encoding="utf-8"))
    shell_kw, str_cmd = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if any(k.arg == "shell" for k in node.keywords):
                shell_kw.append(node.lineno)
            f = node.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "subprocess" and node.args:
                if not isinstance(node.args[0], (ast.List, ast.Tuple)):
                    str_cmd.append((f.attr, node.lineno))
    assert not shell_kw, f"smoke_exe.py 出现 shell= 参数：行 {shell_kw}"
    assert not str_cmd, f"子进程命令必须是列表，发现字符串形态：{str_cmd}"
    assert sx.MIN_STEADY_MB == 150 and sx.STEADY_NEED == 3          # 判据阈值进常量便于回归调参
