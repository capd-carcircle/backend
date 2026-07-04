"""로그인 / 토큰 재발급 통합테스트 (실제 Postgres 테스트 DB 경유)."""
import time


def test_login_success(client, patient_user):
    res = client.post("/api/v1/auth/login", json={
        "phone_number": patient_user.phone_number,
        "password": "test1234",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["user_id"] == patient_user.id
    assert body["name"] == patient_user.name
    assert body["access_token"]
    assert body["refresh_token"]


def test_login_wrong_password(client, patient_user):
    res = client.post("/api/v1/auth/login", json={
        "phone_number": patient_user.phone_number,
        "password": "wrong-password",
    })
    assert res.status_code == 401


def test_login_unknown_phone(client):
    res = client.post("/api/v1/auth/login", json={
        "phone_number": "01099999999",
        "password": "whatever",
    })
    assert res.status_code == 401


def test_refresh_token_rotates_and_blocks_reuse(client, patient_user):
    login_res = client.post("/api/v1/auth/login", json={
        "phone_number": patient_user.phone_number,
        "password": "test1234",
    })
    old_refresh = login_res.json()["refresh_token"]

    # create_refresh_token()의 exp 클레임은 초 단위 해상도라(jti/iat 없음),
    # 로그인과 재발급이 같은 1초 안에 일어나면 sub·role·exp가 전부 같아져서
    # 완전히 동일한 JWT 문자열이 나옴(서명까지 동일). CI가 워낙 빨라 실제로 겪은
    # 케이스라, 최소 1초 이상 벌려서 exp가 갈리도록 함(앱 로직 변경 아님, 테스트 타이밍 보정).
    time.sleep(1)

    res = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert res.status_code == 200
    body = res.json()
    assert body["access_token"]
    assert body["refresh_token"] != old_refresh  # 토큰 회전 확인

    # 회전으로 폐기된 이전 refresh_token 재사용 시도 -> 차단
    reuse_res = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert reuse_res.status_code == 401


def test_me_requires_auth(client):
    res = client.get("/api/v1/auth/me")
    assert res.status_code == 401


def test_me_returns_current_user(client, make_auth_headers, doctor_user):
    res = client.get("/api/v1/auth/me", headers=make_auth_headers(doctor_user))
    assert res.status_code == 200
    assert res.json()["id"] == doctor_user.id
