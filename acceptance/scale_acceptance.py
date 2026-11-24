#!/usr/bin/env python3
"""可生成的离线批处理验收（规格第 3 条：12,004 观测 / 36,000 请求）。

本脚本只依赖标准库与 ``resistor_flow``。它分两阶段：

1. ``generate``（**不计时**）：用现有 ``measurements.publish`` 发布接口与
   数据集构建清单生成一个含 12,004 条标准观测的归档版本，并把清单、参考区间
   表与 36,000 项下游请求写到 ``<root>/materials.json``；
2. ``check``（**计时**）：全新进程读取已准备的归档与资料，调用
   :func:`resistor_flow.calibration.calibration_batch`，逐项核对全部结果，
   打印一行 JSON 摘要（CPU 秒、峰值 RSS、不符项数）。

不带参数运行等价于 ``all``：生成到临时目录，再在 ``ulimit -v 262144``
（256 MiB 地址空间）下启动新进程执行 ``check``。

复现步骤（仓库根目录）::

    python3 acceptance/scale_acceptance.py
    # 或分步：
    python3 acceptance/scale_acceptance.py generate /tmp/q5acc
    bash -c 'ulimit -v 262144; exec python3 -I acceptance/scale_acceptance.py check /tmp/q5acc'

若包已安装，也可直接 ``python3 -I acceptance/scale_acceptance.py check <root>``；
脚本会自行把仓库 ``src/`` 加入 ``sys.path``。
"""

import json
import os
import subprocess
import sys
import tempfile
import time

# 允许直接 `python3 acceptance/scale_acceptance.py` 运行而无需安装包。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, 'src')
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from fractions import Fraction  # noqa: E402

from resistor_flow.measurements import publish  # noqa: E402
from resistor_flow.calibration import calibration_batch  # noqa: E402

RUN_ID = 'scale'
RESISTOR = 'r1'
N_FIT = 12_000
TICK_SHIFT = 10_000
ADDRESS_SPACE_KIB = 262_144  # 256 MiB
CPU_LIMIT_SECONDS = 20.0

_NETWORK = {
    'nodes': [
        {'id': 'supply', 'voltage': '12'},
        {'id': 'middle', 'voltage': None},
        {'id': 'ground', 'voltage': '0'},
    ],
    'resistors': [
        {'id': RESISTOR, 'a': 'supply', 'b': 'middle', 'ohms': '2'},
    ],
}
_REGISTRATION = {
    'station': 'S', 'channel': 'c', 'start_tick': 0,
    'end_tick': 10_000_000, 'resistor_id': RESISTOR,
    'positive_node': 'supply', 'gain': '1', 'offset_volts': '0',
}


def low_decimal(i):
    """精确生成 ``i - i**2/1_000_000`` 的最多 6 位小数字符串。"""
    numerator = i * 1_000_000 - i * i
    return '%d.%06d' % (numerator // 1_000_000, numerator % 1_000_000)


def build_materials():
    specimens = []
    batches = []
    # 前四个行键（tick 0..3）：L 的 x=0、12000，H 的 x=0、12000。
    holdout = [(0, 'L', 0), (1, 'L', 12_000),
               (2, 'H', 0), (3, 'H', 12_000)]
    readings = []
    references = []
    for tick, condition, x in holdout:
        row_id = 'h%d' % tick
        readings.append({'station': 'S', 'row_id': row_id, 'channel': 'c',
                         'tick': tick, 'raw_volts': str(x)})
        specimens.append({'run_id': RUN_ID, 'tick': tick,
                          'resistor_id': RESISTOR,
                          'specimen_id': 'hsp%d' % tick,
                          'condition': condition})
        batches.append({'run_id': RUN_ID, 'station': 'S', 'row_id': row_id,
                        'batch_id': 'hb%d' % tick})
        references.append({'run_id': RUN_ID, 'tick': tick,
                           'resistor_id': RESISTOR,
                           'lower': '-1000', 'upper': '14000'})

    # 12,000 条拟合观测，x=i；每 50 行共用一个样件，来源批次逐行独立，
    # 因而绝不跨组。
    for i in range(1, N_FIT + 1):
        tick = TICK_SHIFT + i
        row_id = 'f%05d' % i
        readings.append({'station': 'S', 'row_id': row_id, 'channel': 'c',
                         'tick': tick, 'raw_volts': str(i)})
        specimens.append({'run_id': RUN_ID, 'tick': tick,
                          'resistor_id': RESISTOR,
                          'specimen_id': 'fitsp%03d' % ((i - 1) // 50),
                          'condition': 'L'})
        batches.append({'run_id': RUN_ID, 'station': 'S', 'row_id': row_id,
                        'batch_id': 'fb%05d' % i})
        references.append({'run_id': RUN_ID, 'tick': tick,
                           'resistor_id': RESISTOR,
                           'lower': low_decimal(i),
                           'upper': str(i + 1000)})

    manifest = {
        'versions': [{'run_id': RUN_ID, 'version': 1}],
        'specimens': specimens,
        'calibration_batches': batches,
        'holdout_counts': {'L': 2, 'H': 2},
    }

    requests = []
    for i in range(1, N_FIT + 1):
        x = str(i)
        requests.append({'request_id': 'abs-L-%05d' % i,
                         'reading': {'condition': 'L', 'x': x},
                         'max_width': '1200'})
        requests.append({'request_id': 'abs-H-%05d' % i,
                         'reading': {'condition': 'H', 'x': x},
                         'max_width': '1200'})
        requests.append({'request_id': 'diff-Li-H0-%05d' % i,
                         'first': {'condition': 'L', 'x': x},
                         'second': {'condition': 'H', 'x': '0'},
                         'max_width': '480'})

    publish_request = {
        'run_id': RUN_ID, 'request_id': 'publish-v1', 'base_version': 0,
        'readings': readings,
        'registrations': [dict(_REGISTRATION)],
        'network': _NETWORK,
    }
    materials = {
        'manifest': manifest,
        'references': references,
        'requests': requests,
        'gain_range': ['0.97', '1.01'],
        'offset_range': ['-1000', '1000'],
    }
    return publish_request, materials


def generate(root):
    store = os.path.join(root, 'store')
    os.makedirs(store, exist_ok=True)
    publish_request, materials = build_materials()
    publish(store, publish_request)
    materials_path = os.path.join(root, 'materials.json')
    with open(materials_path, 'w', encoding='utf-8') as handle:
        json.dump(materials, handle, ensure_ascii=False)
    return store, materials_path


def check(root):
    """计时入口：读取资料、跑完整批处理并逐项核对。"""
    import resource
    materials_path = os.path.join(root, 'materials.json')
    store = os.path.join(root, 'store')

    started = time.process_time()
    with open(materials_path, 'r', encoding='utf-8') as handle:
        materials = json.load(handle)
    result = calibration_batch(
        store,
        materials['manifest'],
        materials['references'],
        materials['gain_range'],
        materials['offset_range'],
        materials['requests'],
    )

    mismatches = verify(result)
    cpu_seconds = time.process_time() - started
    maxrss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    summary = {
        'status': result['status'],
        'request_count': len(result['requests']),
        'mismatches': mismatches,
        'cpu_seconds': round(cpu_seconds, 3),
        'maxrss_kib': maxrss_kib,
        'address_space_limit_kib': ADDRESS_SPACE_KIB,
        'cpu_limit_seconds': CPU_LIMIT_SECONDS,
        'passed': (
            not mismatches
            and result['status'] == 'feasible'
            and cpu_seconds <= CPU_LIMIT_SECONDS
            and maxrss_kib <= ADDRESS_SPACE_KIB
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary['passed'] else 1


def verify(result):
    """逐项核对规格第 3 条给出的全部精确区间与资格覆盖范围。"""
    mismatches = []

    def bad(message):
        if len(mismatches) < 20:
            mismatches.append(message)

    if [c for c in ('L', 'H')
            if result['qualifications'][c]['coverage'] != ['0', '12000']
            or not result['qualifications'][c]['issued']]:
        bad('qualification coverage/issued mismatch')

    by_id = result['items_by_id']
    if len(by_id) != 3 * N_FIT:
        bad('expected %d items, got %d' % (3 * N_FIT, len(by_id)))

    for i in range(1, N_FIT + 1):
        expected_low = Fraction(i) - Fraction(i * i, 1_000_000)
        expected_high = Fraction(i + 1000)
        for prefix in ('abs-L-', 'abs-H-'):
            item = by_id.get('%s%05d' % (prefix, i))
            if item is None:
                bad('missing %s%d' % (prefix, i))
                continue
            if item['status'] != 'delivered' \
                    or Fraction(item['lower']) != expected_low \
                    or Fraction(item['upper']) != expected_high:
                bad('%s%d -> %r' % (prefix, i, item.get('range')))
        item = by_id.get('diff-Li-H0-%05d' % i)
        if item is None:
            bad('missing diff %d' % i)
            continue
        if item['status'] != 'delivered' \
                or Fraction(item['lower']) != Fraction('0.97') * i \
                or Fraction(item['upper']) != Fraction('1.01') * i:
            bad('diff %d -> %r' % (i, item.get('range')))
    return mismatches


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else 'all'

    if mode == 'generate':
        root = argv[1]
        store, materials = generate(root)
        print(json.dumps({'store': store, 'materials': materials}))
        return 0

    if mode == 'check':
        return check(argv[1])

    if mode == 'all':
        root = argv[1] if len(argv) > 1 else tempfile.mkdtemp(
            prefix='calibration-acceptance-')
        print('generating archive/materials under %s (untimed)...' % root,
              file=sys.stderr)
        generate(root)
        script = os.path.abspath(__file__)
        if sys.platform.startswith('linux') and '--no-ulimit' not in argv:
            command = ('ulimit -v %d; exec %s -I %s check %s'
                       % (ADDRESS_SPACE_KIB, sys.executable, script, root))
            completed = subprocess.run(['bash', '-c', command],
                                       capture_output=True, text=True)
        else:
            completed = subprocess.run(
                [sys.executable, '-I', script, 'check', root],
                capture_output=True, text=True)
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        if completed.returncode != 0:
            print('acceptance child failed with code %d'
                  % completed.returncode, file=sys.stderr)
        return completed.returncode

    print('unknown mode %r (expected all|generate|check)' % mode,
          file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
