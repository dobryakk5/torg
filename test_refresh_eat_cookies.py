from refresh_eat_cookies import WANTED


def test_full_servicepipe_cookie_set_is_preserved():
    assert {
        "__js_p_",
        "__jhash_",
        "__jua_",
        "__hash_",
        "__lhash_",
    }.issubset(WANTED)
