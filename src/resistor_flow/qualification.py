"""资格复验与盲测交付入口。

这是 :mod:`resistor_flow.calibration` 的同义模块：本任务的“标定可行集 /
条件资格 / 盲测交付”是同一轮离线批处理，公共实现与完整字段约定见
:mod:`resistor_flow.calibration` 与 ``docs/calibration.md``。
"""

from .calibration import (  # noqa: F401
    MAX_REQUESTS as CALIBRATION_MAX_REQUESTS,
    ITEM_CONDITION_NOT_ISSUED,
    ITEM_DELIVERED,
    ITEM_INSUFFICIENT_PRECISION,
    ITEM_OUT_OF_COVERAGE,
    CalibrationError,
    CalibrationInputError,
    DeliveryError,
    DeliveryInputError,
    FeasibleSpace,
    HoldoutInfeasible,
    QualificationError,
    QualificationInputError,
    calibration_batch,
    calibrate,
    deliver_blind,
    evaluate,
    evaluate_batch,
    parse_references,
    parse_requests,
    qualify_and_deliver,
    run_batch,
)

# 更贴近“签发资格 / 交付”措辞的函数别名。
qualify = calibration_batch
deliver = calibration_batch
run_qualification = calibration_batch

__all__ = [
    'CalibrationError', 'CalibrationInputError',
    'QualificationError', 'QualificationInputError',
    'DeliveryError', 'DeliveryInputError',
    'HoldoutInfeasible', 'FeasibleSpace',
    'calibration_batch', 'qualify_and_deliver', 'run_batch',
    'evaluate_batch', 'calibrate', 'evaluate', 'deliver_blind',
    'qualify', 'deliver', 'run_qualification',
    'parse_references', 'parse_requests',
    'CALIBRATION_MAX_REQUESTS',
    'ITEM_DELIVERED', 'ITEM_CONDITION_NOT_ISSUED',
    'ITEM_OUT_OF_COVERAGE', 'ITEM_INSUFFICIENT_PRECISION',
]
