# -*- coding: utf-8 -*-
"""R839：撇号不是引号——`can't` 把签名词切成 `t encode character`。

为什么改（B046）：线上真实报错
`UnicodeEncodeError: 'gbk' codec can't encode character '\\u2018' …`
抽出来的三个签名词里有一个是 `t encode character`——`can't` 的撇号被当成了引号起点，
一路吃到下一个单引号。它照样能召回（同族故障命中了 errors#5/#6），但去重键跟着字符和
位置一起漂：同一条报错换个非法字符就不算「同签名」，`seen_this_session` 于是失效、
同一会话重复注入同一条。

修法：开引号要求**前面不是字母/数字**（`(?<![A-Za-z0-9])`）。`'gbk'` 前面是空格，照收；
`can't` 的撇号前面是 `n`，不收。只动这一处，权重与其它形态一概不变。

本文件钉五件事：
  1. 那条线上报错不再产出 `t encode character`（也不产出任何「单字母+空格」的半截串）；
  2. 同一条报错换非法字符 / 换位置 → 同签名；
  3. `KeyError: 'user_id'` 这类点名单引号标识符照收（防过度收紧）；
  4. `won't` 之后紧跟的真引号 `'abcde'` 照收；
  5. `'gbk'` 这个 codec 名照收。

「修之前会怎样失败」：第 1 条在旧代码上必红——旧正则从 `can't` 的撇号起吃到下一个单引号。
"""
import brain

UNI = ("UnicodeEncodeError: 'gbk' codec can't encode character '\u2018' in position 0: "
       "illegal multibyte sequence")
# 日志里这个字符有两种落法：真字符（上面），或 repr 出来的转义文本 `\u2018`（下面）。
# 两种都得算同一条报错——第一版只测了转义形态，真字符那条反而红（夹具形状不对，R833 的老坑）。
UNI_ESC = UNI.replace("\u2018", "\\u2018")


def _terms(text):
    return [t for t, _ in brain.error_signature_terms(text)]


def test_apostrophe_not_a_quote():
    terms = _terms(UNI)
    assert "t encode character" not in terms, terms
    junk = [t for t in terms if len(t.split(" ")[0]) == 1]
    assert not junk, f"半截词元又出现了：{junk}"
    assert "gbk" in terms and "unicodeencodeerror" in terms, terms


def test_same_sig_across_illegal_chars():
    other = UNI.replace("\u2018", "\u2019").replace("position 0", "position 7")
    sigs = {brain._error_sig_key(brain.error_signature_terms(t))
            for t in (UNI, UNI_ESC, other)}
    assert len(sigs) == 1, sigs
    assert "\\u2018" not in sigs.pop(), "问题字符本体不该进签名"


def test_quoted_identifier_kept():
    terms = _terms("KeyError: 'user_id'")
    assert "user_id" in terms, terms
    assert dict(brain.error_signature_terms("KeyError: 'user_id'"))["user_id"] == 3


def test_wont_apostrophe():
    terms = _terms("ValueError: won't parse 'abcde' field")
    assert "abcde" in terms, terms
    assert not [t for t in terms if t.startswith("t ")], terms


def test_double_quoted_still_works():
    terms = _terms('json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0) '
                   'in "config.json"')
    assert "jsondecodeerror" in terms or "json.decoder.jsondecodeerror" in terms, terms
    assert any("config.json" in t for t in terms), terms
