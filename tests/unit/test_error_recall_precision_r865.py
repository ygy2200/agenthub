# -*- coding: utf-8 -*-
"""R865 报错召回精度：诊断锚点 vs 被报错提到的对象。

病根（真库复现）：`head: cannot open 'README.md'` 抽出的招牌只有 `README.md`，
于是注出两条只提到 README 的无关记录；混合载荷里 stdout 的文件名（计划书、
_gate_r859b.log）也会被当成签名，把真正的 traceback 挤掉。本文件钉住：
  - 有错误形状行时，签名只从这些行抽（stdout 里的文件名不算身份）；
  - 强锚点（报错成语/错误码/HTTP 4xx-5xx）命中才「一条就够」；
  - 只命中对象词或 `not found` 这类弱锚点时不注入；
  - 登记侧与召回侧仍用同一个签名（R856 的口径不能破）。
"""
import brain

SQL_CREATED_AT = ("Traceback (most recent call last):\n"
                  "  File \"demo.py\", line 1, in <module>\n"
                  "sqlite3.OperationalError: no such column: created_at")
SQL_DATE = ("Traceback (most recent call last):\n"
            "  File \"<stdin>\", line 13, in <module>\n"
            "sqlite3.OperationalError: no such column: date")
HEAD = "head: cannot open 'README.md' for reading: No such file or directory"
MODULE = ("Traceback (most recent call last):\n"
          "  File \"x.py\", line 1, in <module>\n"
          "ModuleNotFoundError: No module named 'mcp_server'")
HTTP_404 = "GET /releases/tags/v2.41.209 404 Not Found"


def _recall(root, tmp_path, text, session):
    """只读探针：count=False 不记曝光/欠账，state 落临时目录。"""
    return brain.error_recall(root, text, tool="Bash", agent="qoder",
                              session=session,
                              state_path=str(tmp_path / f"{session}.json"),
                              count=False)


def _terms_of(text):
    return brain.error_signature_terms(
        brain._error_voice(brain._diagnosis_span(text)))


def test_file_not_found_ignores_filename_only_record(hub_root, tmp_path):
    """对象词（README.md）单独命中旧记录时不再上墙；命中的是写成语的教训。"""
    brain.init_db(hub_root)
    mem = brain.add_memory(
        hub_root, "head/cp 报 no such file or directory 时先看 cwd 与相对路径："
                  "路径错了，不是文件被锁。", kind="lesson", tags="文件不存在,路径", agent="qoder")
    rec = brain.add_record(hub_root, "演示-文档", "qoder", "2026-11-01",
                           "2026-11-01（qoder）· README.md 结构整理",
                           "只提到 README.md 的排版，与 head 失败无关。")
    r = _recall(hub_root, tmp_path, HEAD, "head")
    assert mem in r["memory_ids"], (r["memory_ids"], r["text"])
    assert rec not in r["record_ids"], (r["record_ids"], r["text"])
    assert "no such file or directory" in r["sig"], r["sig"]


def test_stdout_noise_does_not_shape_signature(hub_root, tmp_path):
    """混合载荷只从 traceback 抽签名；stdout 里的文件名不能再带出旧记录。"""
    brain.init_db(hub_root)
    decoy = brain.add_record(
        hub_root, "演示-计划", "qoder", "2026-11-01",
        "2026-11-01（qoder）· 迭代计划与门禁日志",
        "agenthub迭代执行计划.md 与 _gate_r859b.log 的归档说明。")
    truth = brain.add_memory(
        hub_root, "errors 表没有 date 列；时间列是 ts / last_ts，查表前先 PRAGMA table_info(errors)。",
        kind="lesson", tags="sqlite,no such column,errors", agent="qoder")
    payload = ("('2026-11-04 agenthub迭代执行计划.md _gate_r859b.log v2.41.212',)\n"
               + SQL_DATE)
    r = _recall(hub_root, tmp_path, payload, "mixed")
    assert "_gate_r859b.log" not in r["sig"] and "执行计划.md" not in r["sig"], r["sig"]
    assert "no such column: date" in r["sig"], r["sig"]
    assert decoy not in r["record_ids"], (r["record_ids"], r["text"])
    assert truth in r["memory_ids"], (r["memory_ids"], r["text"])


def test_shell_idiom_enters_signature():
    """没有异常名的 shell 失败也要有身份：成语进签名，引号文件名退成对象。"""
    d = dict(_terms_of(HEAD))
    assert d.get("no such file or directory") == 3, d
    assert d.get("cannot open") == 3, d
    sig = brain.error_sig_of(HEAD)
    assert "no such file or directory" in sig and "cannot open" in sig, sig


def test_object_only_match_rejected_when_anchor_exists():
    terms = _terms_of(HEAD)
    assert brain._err_accept(terms, "readme.md 的目录说明") is False, \
        "只对文件名不能认定是同一条报错"
    assert brain._err_accept(terms, "no such file or directory：先看 cwd") is True


def test_not_found_alone_is_weak_but_http_code_carries_identity():
    """`not found` 是弱锚点；`404` 才是 HTTP 失败的身份。"""
    d = dict(_terms_of(HTTP_404))
    assert d.get("404") == 3 and d.get("not found") == 3, d
    terms = _terms_of(HTTP_404)
    assert brain._err_accept(terms, "not found 的某个地方") is False
    assert brain._err_accept(terms, "Release 草稿列表端点 404 该怎么修") is True


def test_module_name_alone_does_not_match_module_not_found(hub_root, tmp_path):
    brain.init_db(hub_root)
    decoy = brain.add_memory(hub_root, "mcp_server 部署路径是 ~/.agenthub/mcp_server。",
                             kind="fact", tags="部署", agent="qoder")
    truth = brain.add_memory(hub_root, "No module named 'mcp_server'：把部署目录加进 "
                                       "sys.path 再 import。", kind="lesson",
                             tags="mcp_server,import", agent="qoder")
    r = _recall(hub_root, tmp_path, MODULE, "module")
    assert truth in r["memory_ids"], (r["memory_ids"], r["text"])
    assert decoy not in r["memory_ids"], (r["memory_ids"], r["text"])


def test_headline_prefers_exception_line():
    """「这次失败」展示报错本身，而不是恰好含招牌词的 stdout 行。"""
    payload = ("('agenthub迭代执行计划.md _gate_r859b.log',)\n" + SQL_DATE)
    diag = brain._diagnosis_span(payload)
    high = [t for t, w in _terms_of(payload) if w >= 3]
    line = brain._error_headline(brain._error_voice(diag), high)
    assert "sqlite3.OperationalError" in line and "no such column: date" in line, line
    assert "计划.md" not in line, line


def test_registered_same_error_still_hits_exactly(hub_root, tmp_path):
    """R857 的回环不能破：登记过的同一条报错，召回必须给出现成修法。"""
    brain.init_db(hub_root)
    info = brain.error_add(hub_root, "qoder", "errors 表写了 created_at",
                           detail=SQL_CREATED_AT,
                           undo="改用 last_ts；查表前先 PRAGMA table_info(errors)",
                           return_info=True)
    eid = int(info["id"])
    r = _recall(hub_root, tmp_path, SQL_CREATED_AT, "exact")
    assert eid in r["error_ids"], (r["error_ids"], r["text"])
    assert "登记过同类故障" in r["text"], r["text"]


def test_signature_is_identical_on_both_sides():
    """登记正文带说明、召回是裸报错——同一函数的输入形状仍要对齐（R856）。"""
    detail = "报错原文：\n" + SQL_CREATED_AT + "\n处理：先 PRAGMA table_info(errors)，时间列是 last_ts。"
    assert brain.error_sig_of(detail) == brain.error_sig_of(SQL_CREATED_AT)
    assert brain.error_sig_of(SQL_CREATED_AT) == brain.error_sig_of("失败调用：python demo.py\n" + SQL_CREATED_AT)
