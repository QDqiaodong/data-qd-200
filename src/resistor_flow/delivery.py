"""盲测取数交付入口（:mod:`resistor_flow.calibration` 的同义模块）。

资格裁决与交付在同一轮批处理内完成；字段、状态与复现步骤见
``docs/calibration.md``。
"""

from .calibration import (  # noqa: F401
    ITEM_CONDITION_NOT_ISSUED,
    ITEM_DELIVERED,
    ITEM_INSUFFICIENT_PRECISION,
    ITEM_OUT_OF_COVERAGE,
    CalibrationError as DeliveryBatchError,
    CalibrationInputError as DeliveryInputError,
    calibration_batch,
    deliver_blind,
)

deliver_batch = calibration_batch
deliver = calibration_batch

__all__ = [
    'DeliveryBatchError', 'DeliveryInputError',
    'calibration_batch', 'deliver_batch', 'deliver', 'deliver_blind',
    'ITEM_DELIVERED', 'ITEM_CONDITION_NOT_ISSUED',
    'ITEM_OUT_OF_COVERAGE', 'ITEM_INSUFFICIENT_PRECISION',
]
