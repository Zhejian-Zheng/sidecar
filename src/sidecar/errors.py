"""Fixed observation diagnostics safe to persist and send across device RPC."""

from contextlib import contextmanager

OBSERVATION_ERRORS = {
    "connection": "设备连接失败，请检查设备连接后重试",
    "display_verify": "执行屏校验失败，请检查副屏是否仍然可用",
    "foreground": "无法读取执行屏当前应用，请核对执行画面",
    "hierarchy": "界面读取失败，请核对应用页面后重试",
    "screenshot": "截图失败，请检查执行屏和截图工具后重试",
    "page_changed": "读取期间页面发生变化，请等待页面稳定后重试",
    "release": "界面读取连接释放失败，请检查设备连接后重试",
    "transport": "界面读取通信失败或超时，请检查设备连接后重试",
    "unknown": "界面读取失败，未能确定具体阶段，请核对执行画面",
}


class ObservationError(RuntimeError):
    def __init__(self, stage):
        # Never trust exception text or arbitrary stage names received over RPC.
        self.stage = stage if isinstance(stage, str) and stage in OBSERVATION_ERRORS else "unknown"
        super().__init__(OBSERVATION_ERRORS[self.stage])


@contextmanager
def observation_stage(stage):
    try:
        yield
    except ObservationError:
        raise
    except Exception as exc:
        raise ObservationError(stage) from exc
