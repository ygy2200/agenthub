# -*- coding: utf-8 -*-
"""R859·守卫：真跑钩子的用例必须把 `AGENTHUB_ROOT` 钉在临时目录上。

起因不是猜的。12:55 真实大脑（`D:\\agent资料\\zcode项目记录`）的 `fail_events` 里出现两行
`bad_json`/`null_event` 样本，`head` 分别是 `{not json` 和 `null`，`agent`/`tool` 全空——
这两个字符串只存在于 `tests/unit/test_error_recall_r833.py::test_mjs_survives_garbage_input`。
那条用例喂烂 stdin 时 env 里没给 `AGENTHUB_ROOT`，钩子于是从 `config.agents.json`
解析出**真实根**去写库。R859 之前这种 payload 只往本地日志打一行，谁也不疼；采集面
上线后它开始往主人的大脑里写行——**用例污染了宿主**，而 B067 的全部意义就是读这张表的
形状分布，分布里掺着测试自己喂的样本，下一轮的结论就不可信。

同类坑以前踩过一次（`test_fail_samples_r859.py::test_hit_does_not_sample` 的
`state_path`：默认状态写进 `~/.workbuddy/state/`，第二次跑同一个签名直接
`seen_this_session`）。那次是状态文件，这次是数据根——同一个形状：**子进程不继承 pytest
的隔离**，`os.environ` 里有什么它就照用什么。

所以这条守卫扫的是**结构**而不是词表：AST 找出每一个 `subprocess.run`/`Popen` 调用，
源码段里提到钩子（`.mjs`）的，必须在同一次调用的 env 里显式出现 `AGENTHUB_ROOT`。
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]


def _segment_of(src: str, node: ast.AST) -> str:
    lines = src.splitlines()
    end = getattr(node, "end_lineno", node.lineno)
    return "\n".join(lines[node.lineno - 1:end])


def _scan(tests_dir: Path, label_base: Path):
    """扫 tests_dir 下所有用例：真跑钩子却没钉 `AGENTHUB_ROOT` 的函数，返回点名清单。"""
    offenders = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        if ".mjs" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            calls_hook = any(
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run", "Popen", "call", "check_output")
                and ".mjs" in _segment_of(src, node)
                for node in ast.walk(fn))
            if calls_hook and "AGENTHUB_ROOT" not in _segment_of(src, fn):
                offenders.append(f"{path.relative_to(label_base)}:{fn.lineno} {fn.name}()")
    return offenders


def test_every_hook_subprocess_call_pins_the_brain_root():
    """每个真跑钩子的用例函数，必须在**这个函数里**把 `AGENTHUB_ROOT` 钉死。

    判定落在函数级而不是调用级，是因为 `env = {...}; subprocess.run(..., env=env)`
    这种写法（r811/r820 就是这么写的）调用段里没有字面量——按调用段查会把已经
    钉好的用例误报成漏钉。函数级仍然咬得住本轮那种「整条用例没给根」的形状。
    """
    offenders = _scan(TESTS, TESTS.parent)
    assert not offenders, (
        "这些用例子进程会跑到真实大脑根上（钩子从 config.agents.json 解析根目录），"
        "把测试样本写进主人的库里：" + "、".join(offenders))


def test_guard_bites_on_a_hook_call_without_the_root(tmp_path):
    """反证：守卫不是「过了就没人看」。造一条没钉 AGENTHUB_ROOT 的钩子调用，它必须点名。"""
    fake = tmp_path / "tests" / "unit"
    fake.mkdir(parents=True)
    (fake / "test_sneaky.py").write_text(
        'import subprocess\n'
        'def test_sneaky():\n'
        '    subprocess.run(["node", "brain_error_recall.mjs"], input="null",\n'
        '                   env={"A": "1"})\n'
        '\n'
        'def test_ok_root_pinned():\n'
        '    env = {"AGENTHUB_ROOT": "/tmp/hub"}\n'
        '    subprocess.run(["node", "brain_brief.mjs"], env=env)\n',
        encoding="utf-8")
    hit = _scan(fake, tmp_path)
    assert any("test_sneaky" in o for o in hit), f"反证用例没被点出来：{hit}"
    assert not any("test_ok_root_pinned" in o for o in hit), \
        f"`env = {{...}}` 分开写的钉根用例被误伤：{hit}"
