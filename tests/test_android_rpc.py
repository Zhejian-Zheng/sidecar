import pytest


@pytest.mark.parametrize("method", ["click", "setText", "swipe", "pressKey"])
def test_device_write_transport_does_not_retry_uncertain_calls(monkeypatch, method):
    u2 = pytest.importorskip("uiautomator2")
    from uiautomator2 import core
    from uiautomator2.exceptions import HTTPError

    from sidecar import android_rpc

    class Device(core.BasicUiautomatorServer):
        def __init__(self):
            self._dev = object()
            self._device_server_port = 9008
            self._debug = False

        def stop_uiautomator(self):
            pass

        def start_uiautomator(self):
            pass

    calls = []

    def uncertain_write(dev, port, operation, params, timeout, debug):
        calls.append(operation)
        raise HTTPError("write may already have succeeded")

    monkeypatch.setattr(u2, "connect", lambda serial: Device())
    monkeypatch.setattr(core, "_jsonrpc_call", uncertain_write)
    device = android_rpc.connect_device("emulator-test")
    with pytest.raises(HTTPError):
        device.jsonrpc_call(method, [1, 2])
    assert calls == [method]


@pytest.mark.parametrize("foreground", [True, False])
def test_launch_checks_foreground_instead_of_only_running_process(foreground):
    from types import SimpleNamespace

    from sidecar.android_rpc import launch_app

    class Device:
        activity = None

        def shell(self, args):
            return SimpleNamespace(output="com.test.clock/.Clock\n", exit_code=0)

        def app_start(self, package, *, activity, wait, stop):
            self.activity = activity

        def app_wait(self, package, *, timeout, front):
            return 42 if front and foreground else 0

    d = Device()
    if foreground:
        launch_app(d, "com.test.clock")
        assert d.activity == ".Clock"
    else:
        with pytest.raises(RuntimeError):
            launch_app(d, "com.test.clock")


def test_read_only_rpc_can_recover_connection(monkeypatch):
    u2 = pytest.importorskip("uiautomator2")
    from uiautomator2 import core
    from uiautomator2.exceptions import HTTPError

    from sidecar.android_rpc import connect_device

    class Device(core.BasicUiautomatorServer):
        def __init__(self):
            self._dev = object()
            self._device_server_port = 9008
            self._debug = False

        def stop_uiautomator(self):
            pass

        def start_uiautomator(self):
            pass

    calls = []

    def read(dev, port, method, params, timeout, debug):
        calls.append(method)
        if len(calls) == 1:
            raise HTTPError("temporary read failure")
        return "<hierarchy/>"

    monkeypatch.setattr(u2, "connect", lambda serial: Device())
    monkeypatch.setattr(core, "_jsonrpc_call", read)
    assert connect_device("emulator-test").jsonrpc_call("dumpWindowHierarchy") == "<hierarchy/>"
    assert calls == ["dumpWindowHierarchy", "dumpWindowHierarchy"]
