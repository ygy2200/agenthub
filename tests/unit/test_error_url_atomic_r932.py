# -*- coding: utf-8 -*-
"""R932：URL 是一个身份单元；URL 内 IP/IP:port 不再重复占位，外部地址仍保留。"""
import brain


def _tokens(text):
    return [term for term, _weight in brain.error_signature_terms(text)]


def test_ip_url_keeps_host_port_but_not_duplicate_bare_ip():
    tokens = _tokens("GET http://127.0.0.1:7890/repos/x 504")
    assert tokens.count("127.0.0.1:7890") == 1, tokens
    assert "127.0.0.1" not in tokens, tokens


def test_url_and_bare_same_endpoint_converge():
    from_url = brain.error_sig_of("ConnectionError: refused http://127.0.0.1:7890/api")
    bare = brain.error_sig_of("ConnectionError: refused 127.0.0.1:7890")
    assert from_url and from_url == bare, (from_url, bare)


def test_hostname_and_port_form_one_identity():
    tokens = _tokens("ConnectionError: https://api.github.com:8443/repos/x")
    assert "api.github.com:8443" in tokens, tokens
    assert "api.github.com" not in tokens, tokens


def test_different_ports_and_hosts_still_have_different_keys():
    base = "ConnectionError: refused http://127.0.0.1:{}/api"
    assert brain.error_sig_of(base.format(7890)) != brain.error_sig_of(base.format(7891))
    a = brain.error_sig_of("ConnectionError: https://api.github.com/x")
    b = brain.error_sig_of("ConnectionError: https://example.com/x")
    assert a and b and a != b


def test_url_path_and_query_remain_volatile():
    a = brain.error_sig_of("ConnectionError: http://127.0.0.1:7890/api/a?x=1")
    b = brain.error_sig_of("ConnectionError: http://127.0.0.1:7890/api/b?x=2")
    assert a and a == b


def test_bare_ip_outside_url_still_signs():
    tokens = _tokens("ConnectionError: 127.0.0.1:7890")
    assert "127.0.0.1:7890" in tokens, tokens


def test_unrelated_proxy_address_beside_url_is_not_erased():
    tokens = _tokens(
        "ConnectionError: https://api.github.com/repos/x via 127.0.0.1:7890")
    assert "api.github.com" in tokens, tokens
    assert "127.0.0.1:7890" in tokens, tokens


def test_error_add_stores_atomic_url_signature(hub_root):
    brain.init_db(hub_root)
    info = brain.error_add(
        hub_root, "qoder", "本地服务连接失败",
        detail="ConnectionError: refused http://127.0.0.1:7890/api",
        undo="检查服务", return_info=True)
    with brain.db_conn(hub_root) as conn:
        sig = conn.execute("SELECT sig FROM errors WHERE id=?", (info["id"],)).fetchone()[0]
    assert "127.0.0.1:7890" in sig, sig
    assert "127.0.0.1" not in sig.split("|"), sig
