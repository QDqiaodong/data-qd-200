"""盲测取数别名模块；实现与字段约定见 :mod:`resistor_flow.calibration`。"""

from .calibration import (  # noqa: F401
    ITEM_CONDITION_NOT_ISSUED,
    ITEM_DELIVERED,
    ITEM_INSUFFICIENT_PRECISION,
    ITEM_OUT_OF_COVERAGE,
    CalibrationInputError as BlindInputError,
    calibration_batch,
    deliver,
    deliver_blind,
)

blind_deliver = calibration_batch
run_blind_batch = calibration_batch

__all__ = [
    'BlindInputError', 'calibration_batch', 'deliver', 'deliver_blind',
    'blind_deliver', 'run_blind_batch',
    'ITEM_DELIVERED', 'ITEM_CONDITION_NOT_ISSUED',
    'ITEM_OUT_OF_COVERAGE', 'ITEM_INSUFFICIENT_PRECISION',
]
