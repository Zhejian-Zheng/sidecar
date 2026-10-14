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


TEXT_INPUT_ERRORS = {
    "display_verify": "文字输入前的执行屏校验失败，尚未尝试写入",
    "prepare": "文字输入桥接准备失败，尚未尝试写入",
    "connection": "文字输入桥接连接失败，尚未尝试写入",
    "target": "目标输入框无法唯一匹配或已变化，尚未尝试写入",
    "write": "文字写入调用失败，无法确认是否生效，请核对输入框后继续",
    "write_rejected": "目标输入框拒绝了文字写入，请核对输入框后继续",
    "verification": "文字写入调用已返回成功，但写入后的核验未通过，请核对输入框后继续",
    "post_display_verify": "文字写入后的执行屏校验失败，请核对设备上的输入结果",
    "transport": "文字输入桥接调用失败或超时，写入结果未知，请核对输入框后继续",
    "protocol": "文字输入桥接未返回有效结论，写入结果未知，请核对输入框后继续",
    "unknown": "文字输入失败，未能确定具体阶段，请核对输入框后继续",
}


class TextInputError(RuntimeError):
    def __init__(self, stage):
        self.stage = stage if isinstance(stage, str) and stage in TEXT_INPUT_ERRORS else "unknown"
        super().__init__(TEXT_INPUT_ERRORS[self.stage])
