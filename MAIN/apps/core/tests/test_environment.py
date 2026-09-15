def test_test_environment_fixture_is_active(settings):
    assert settings.APP_ENV == "test"
    assert settings.SECRET_KEY == "test-only-secret-key-not-for-production"
