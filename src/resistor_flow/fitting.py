"""拟合可行参数集别名模块。

整轮“拟合 -> 资格复验 -> 盲测交付”仍由
:func:`resistor_flow.calibration.calibration_batch` 完成；本模块额外暴露精确
二维可行集构造器，便于只做拟合几何核对。完整约定见 ``docs/calibration.md``。
"""

from .calibration import (  # noqa: F401
    CalibrationInputError as FittingInputError,
    FeasibleSpace,
    calibration_batch,
    calibrate,
)

fit_and_qualify = calibration_batch
build_feasible_space = FeasibleSpace

__all__ = [
    'FittingInputError', 'FeasibleSpace', 'build_feasible_space',
    'calibration_batch', 'calibrate', 'fit_and_qualify',
]
