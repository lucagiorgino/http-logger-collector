import pytest

from integration_tests.stack_client import load_settings, run_stack


def test_threaded_devices_authenticate_upload_and_read_persisted_logs():
    settings = load_settings()
    if settings is None:
        pytest.skip("Set STACK_TEST_ENABLED=true and configure two dedicated OAuth test clients")
    run_stack(settings)
