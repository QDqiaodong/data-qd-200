"""可生成的冷启动装配批处理验收（规格第 3 条）。

两阶段：

1. ``generate``（**不计时**）：只用制模接口生成普通包与一件低限额定件的同电性
   修订包，再把 48 个两端口实例串成 N0..N48、4,096 条激励写成
   ``<root>/packages.json`` 与 ``<root>/batch.json``。**不计算也不保存**任何
   装配响应。
2. ``check``（**计时**）：全新进程读取已制好的包与批次输入，调用装配批处理，
   逐项核对两份完整结果（通过样本的网电位/端口电流、被拒样本的全部超额件），
   打印一行 JSON 摘要（CPU 秒、峰值 RSS、不符项数）。

不带参数运行等价于 ``all``：生成到临时目录，再在 ``ulimit -v 262144``
（256 MiB 地址空间）下启动新进程执行 ``check``。

复现步骤（仓库根目录）::

    python3 acceptance/assembly_acceptance.py
    # 或分步：
    python3 acceptance/assembly_acceptance.py generate /tmp/asmacc
    bash -c 'ulimit -v 262144; exec python3 -I acceptance/assembly_acceptance.py check /tmp/asmacc'
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from fractions import Fraction

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, 'src')
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from resistor_flow.modules import build_module_package  # noqa: E402
from resistor_flow.assembly import assemble_batch  # noqa: E402

ADDRESS_SPACE_KIB = 262_144  # 256 MiB
CPU_LIMIT_SECONDS = 35.0

N_MODULES = 48
N_NETS = N_MODULES + 1
N_SAMPLES = 4_096
ACCEPT_LIMIT = 2_880
LOW_INDEX = 17          # 低限额定件所在实例
LOW_RESISTOR = 'r11'    # 低限额定件的电阻 ID


def _chain(rated):
    """12 个 1 Ω 电阻串联的两端口模块；``rated`` 为各件额定值列表或单值。"""
    nodes = ['p'] + ['n%d' % k for k in range(1, 12)] + ['q']
    resistors = []
    for k in range(12):
        rating = rated if isinstance(rated, str) else rated[k]
        resistors.append({'id': 'r%d' % k, 'a': nodes[k], 'b': nodes[k + 1],
                          'ohms': '1', 'rated_watts': rating})
    return {'nodes': nodes, 'ports': ['p', 'q'], 'resistors': resistors}


def _decimal(value):
    """精确有理数 -> 有界十进制字符串（分母整除 10^6）。"""
    assert 10 ** 6 % value.denominator == 0, value
    scaled = value.numerator * (10 ** 6 // value.denominator)
    if scaled % 10 ** 6 == 0:
        return str(scaled // 10 ** 6)
    return ('%d.%06d' % (scaled // 10 ** 6, scaled % 10 ** 6)).rstrip('0')


def build_materials():
    normal = _chain('100')
    normal.update({'model': 'CH', 'revision': 'NORMAL'})
    low_ratings = ['100'] * 12
    low_ratings[11] = '1'
    low = _chain(low_ratings)
    low.update({'model': 'CH', 'revision': 'LOW'})
    packages = [build_module_package(normal), build_module_package(low)]

    instances = []
    for k in range(N_MODULES):
        instances.append({
            'instance': 'I%02d' % k,
            'model': 'CH',
            'revision': 'LOW' if k == LOW_INDEX else 'NORMAL',
            'bindings': {'p': 'N%d' % k, 'q': 'N%d' % (k + 1)},
        })
    excitations = []
    for i in range(1, N_SAMPLES + 1):
        excitations.append({
            'sample_id': 's%04d' % i,
            'voltages': {'N0': '0', 'N48': _decimal(Fraction(i, 5))},
        })
    batch = {
        'driven_nets': ['N48', 'N0'],
        'instances': instances,
        'excitations': excitations,
    }
    return packages, batch


def generate(root):
    os.makedirs(root, exist_ok=True)
    packages, batch = build_materials()
    packages_path = os.path.join(root, 'packages.json')
    batch_path = os.path.join(root, 'batch.json')
    with open(packages_path, 'w', encoding='utf-8') as handle:
        json.dump(packages, handle, ensure_ascii=False)
    with open(batch_path, 'w', encoding='utf-8') as handle:
        json.dump(batch, handle, ensure_ascii=False)
    return packages_path, batch_path


def check(root):
    import resource
    packages_path = os.path.join(root, 'packages.json')
    batch_path = os.path.join(root, 'batch.json')

    started = time.process_time()
    with open(packages_path, 'r', encoding='utf-8') as handle:
        packages = json.load(handle)
    with open(batch_path, 'r', encoding='utf-8') as handle:
        batch = json.load(handle)
    result = assemble_batch(packages, batch['instances'],
                            batch['driven_nets'], batch['excitations'])

    mismatches = verify(result)
    cpu_seconds = time.process_time() - started
    maxrss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    summary = {
        'accepted': result['accepted']['sample_count'],
        'rejected': result['rejected']['sample_count'],
        'mismatches': mismatches,
        'cpu_seconds': round(cpu_seconds, 3),
        'maxrss_kib': maxrss_kib,
        'address_space_limit_kib': ADDRESS_SPACE_KIB,
        'cpu_limit_seconds': CPU_LIMIT_SECONDS,
        'passed': (not mismatches
                   and result['accepted']['sample_count'] == ACCEPT_LIMIT
                   and result['rejected']['sample_count']
                   == N_SAMPLES - ACCEPT_LIMIT
                   and cpu_seconds <= CPU_LIMIT_SECONDS
                   and maxrss_kib <= ADDRESS_SPACE_KIB),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary['passed'] else 1


def verify(result):
    mismatches = []

    def bad(message):
        if len(mismatches) < 20:
            mismatches.append(message)

    accepted = result['accepted']
    rejected = result['rejected']
    nets = accepted['assembly']['nets']
    if sorted(int(name[1:]) for name in nets) != list(range(N_NETS)):
        bad('unexpected net list: %r' % (nets,))

    if [s['sample_id'] for s in accepted['samples']] != \
            ['s%04d' % i for i in range(1, ACCEPT_LIMIT + 1)]:
        bad('accepted sample set is not s0001..s2880')

    for sample in accepted['samples']:
        i = int(sample['sample_id'][1:])
        expected_v = {net: str(Fraction(i * int(net[1:]), 240))
                      for net in nets}
        if {net: Fraction(v) for net, v in sample['voltages'].items()} != \
                {net: Fraction(v) for net, v in expected_v.items()}:
            bad('%s net potentials differ' % sample['sample_id'])
        magnitude = Fraction(i, 2880)
        currents = sample['currents']
        if sorted(currents) != ['I%02d' % k for k in range(N_MODULES)]:
            bad('%s instance currents missing' % sample['sample_id'])
            continue
        for k in range(N_MODULES):
            pair = currents['I%02d' % k]
            if pair.get('p') != str(-magnitude) \
                    or pair.get('q') != str(magnitude):
                bad('%s adapter %02d currents differ' % (sample['sample_id'], k))

    if [s['sample_id'] for s in rejected['samples']] != \
            ['s%04d' % i for i in range(ACCEPT_LIMIT + 1, N_SAMPLES + 1)]:
        bad('rejected sample set is not s2881..s4096')

    for sample in rejected['samples']:
        i = int(sample['sample_id'][1:])
        expected = [{
            'instance': 'I%02d' % LOW_INDEX,
            'model': 'CH',
            'revision': 'LOW',
            'resistor_id': LOW_RESISTOR,
            'watts': str(Fraction(i, 2880) ** 2),
            'rated_watts': '1',
        }]
        if sample['over_rating'] != expected:
            bad('%s over-rating list differs: %r'
                % (sample['sample_id'], sample['over_rating']))
        if 'voltages' in sample or 'currents' in sample:
            bad('%s rejected row carries executable port sample'
                % sample['sample_id'])
    return mismatches


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else 'all'

    if mode == 'generate':
        root = argv[1]
        packages_path, batch_path = generate(root)
        print(json.dumps({'packages': packages_path, 'batch': batch_path}))
        return 0

    if mode == 'check':
        return check(argv[1])

    if mode == 'all':
        root = argv[1] if len(argv) > 1 else tempfile.mkdtemp(
            prefix='assembly-acceptance-')
        print('generating packages/batch under %s (untimed)...' % root,
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
