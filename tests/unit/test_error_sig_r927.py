# -*- coding: utf-8 -*-
r"""R927：报错签名里两类「不配当身份」的词——URL 被清洗器咬碎、指针地址当招牌。

签名是这条通道的全部：`error_sig_of` 稳定，登记一次下次同签名才秒命中（R834 的承诺）。
两类词把它打掉，真库取证（2026-11-06，`searches` 里 tool=error_recall_miss）：

  · **URL 被 `_error_clean` 咬碎**。第 10026 行那条「去路径前缀」`/(?:[\w.-]+/){2,}`
    不分 URL：`cannot GET https://api.github.com/repos/x/y 504` 洗成
    `cannot GET https:/ 504`——主机名（这条报错唯一的身分）没了，剩下 `https:/` 这种
    每条抓网报错都有的常数。账上实锤：`https:/ memory`、`https:/ simplemem|simplemem`、
    `https:/ 2606.14571` 三条欠账，前缀全是它。
    只把碎片挡掉还不够：那样所有「连不上」都收成同一个签名，把「GitHub 要走镜像」的修法
    安到「本地服务没起」上——**误配比空转更坏**。所以主机要补回身份位（跟
    `_ERR_IPPORT_RE` 同一档 w=2，127.0.0.1:7890 早就是身份了）。

  · **指针地址进签名**。`_ERR_CODE_RE` 收 `0x[0-9A-Fa-f]{6,16}`（这是给 NTSTATUS/EACCES
    那种真错误码的），Python 的 `<generator object ... at 0x000001C2896D3AC0>` 于是也算
    错误码，且排序时抢在别词前面。地址每个进程都变 → 同一条报错每次都是新签名 →
    欠账永不收编、修法永不复命中。真库实锤（就在本轮）：
    `0x000001c2896d3ac0|syntaxerror|test_dirty_row_is_resigned_and_old_value_kept|unicodeescape`。
    挡的是**指针形状**（0x + ≥12 位十六进制，64 位地址），8 位的 0xC0000005 照旧算身份。

判据写在前面，跑完不挑口径。
"""
import re

import brain

JUNK_HOST = re.compile(r"^https?:?/*$")
POINTER = re.compile(r"^0x0*[0-9a-f]{9,}$")   # 0x + 12~16 位（去掉前导 0 后仍 ≥9）


def _terms(text):
    return brain.error_signature_terms(text)


def _toks(text):
    return [t for t, _w in _terms(text)]


def test_url_host_survives_the_cleaner():
    """清洗器不许再把 URL 咬成 `https:/`。"""
    t = ("cannot GET https://api.github.com/repos/x/y/releases 504 Gateway Timeout")
    cleaned = brain._error_clean(t)
    assert not re.search(r"https?:/\s", cleaned), cleaned
    assert "api.github.com" in cleaned, cleaned


def test_url_host_is_an_identity_term():
    """主机名要和 IP:port 同档进签名（w=2）——否则所有网络报错塌成同一个键。"""
    toks = _toks("fetch failed for https://api.github.com/repos/x/y: ConnectionError")
    assert "api.github.com" in toks, toks
    w = dict(_terms("fetch failed for https://api.github.com/repos/x/y: ConnectionError"))
    assert w["api.github.com"] == 2, w


def test_different_hosts_do_not_share_a_signature():
    a = brain.error_sig_of("fetch failed for https://api.github.com/x: ConnectionError")
    b = brain.error_sig_of("fetch failed for https://127.0.0.1:11434/x: ConnectionError")
    assert a and b and a != b, (a, b)


def test_scheme_fragment_never_signed():
    """`https:/` 这种常数碎片不许出现在任何签名词里（每条抓网报错都有它=零区分度）。

    真库那三条欠账（`https:/ memory`、`https:/ simplemem|simplemem`、`https:/ 2606.14571`）
    是成语/引号正则连着被咬碎的 URL 尾巴一起收进去的，所以判据按「含碎片」算，
    不能只挡整词等于 `https:/` 的那种。
    """
    frag = re.compile(r"https?:/(\s|$)")
    for s in ("cannot GET https://api.github.com/repos/x/y 504",
              "urllib.error.URLError: <urlopen error https://simplemem.io/api/v1/mem>",
              "sync failed: not found https://memory.dev/api 500 for 'https:/ memory'"):
        bad = [t for t in _toks(s) if frag.search(t)]
        assert not bad, (s, bad)


def test_pointer_address_is_not_an_identity():
    head = ("Traceback: E + where False = any(<generator object "
            "test_dirty_row_is_resigned_and_old_value_kept.<locals>.<genexpr> at ")
    a = brain.error_sig_of(head + "0x000001C2896D3AC0>) SyntaxError: (unicode error) "
                              "'unicodeescape' codec can't decode")
    b = brain.error_sig_of(head + "0x000002FFAABBCC11>) SyntaxError: (unicode error) "
                              "'unicodeescape' codec can't decode")
    assert a and b, (a, b)
    assert a == b, f"地址换了签名就换——同一条报错永远收不了账：{a} vs {b}"
    assert not [t for t in _toks(head + "0x000001C2896D3AC0>)") if POINTER.match(t)]


def test_real_error_codes_still_sign():
    """收紧不许误伤真错误码：NTSTATUS/HRESULT/EACCES 都还是身份。"""
    assert "0xc0000005" in _toks("AccessViolation: reading at 0xC0000005 in native.dll")
    assert "eacces" in _toks("open failed: EACCES (Permission denied)")
    assert "0x80070005" in _toks("Windows Error 0x80070005: Access is denied")


def test_no_signature_words_in_library_rows(hub_root):
    """落库通道也得干净：登记一条带 URL/带地址的报错，签名里不该有这两类词。"""
    brain.init_db(hub_root)
    info = brain.error_add(hub_root, "qoder", "抓 GitHub 失败",
                           detail="cannot GET https://api.github.com/repos/x/y 504",
                           undo="换镜像", return_info=True)
    eid = info["id"]
    with brain.db_conn(hub_root) as conn:
        sig = conn.execute("SELECT sig FROM errors WHERE id=?", (eid,)).fetchone()[0]
    assert "api.github.com" in sig, sig
    assert not [p for p in sig.split("|") if JUNK_HOST.match(p) or POINTER.match(p)], sig
