"""成长任务一键执行（调用上游自带的 `task_runner.py`）。

为什么要做这个
--------------
上游 `workbuddy2api` 自带 `scripts/task_runner.py`，能覆盖约 20 种成长中心任务
（模板 / 专家 / 技能 / 自动化 / Buddy / 画布 / 各类对话…），但：

  · 它被上游定性为**辅助工具**（README「辅助工具」一节），不是定时任务；
  · 上游调度器只自动跑其中 **6 类**（签到 / 活跃地图 / 猫猫旅行 / 保活 /
    开学季 / 夜猫），成长中心那批**没有任何自动入口**；
  · 本面板此前是纯只读的（只采集展示日志），点不了任何任务。

于是这些任务只能上服务器手敲命令，这就是 issue #19 的诉求。

本模块只负责**调用上游那个脚本**，不自己实现上报协议 —— 脚本是「与官方客户端
逐字对齐」的那份参照实现，自己重写一份必然漂移。

三种模式（按风险分级，不允许任意参数）
------------------------------------
  · `preview` —— dry-run，**只读**：只查询并展示「哪些能点亮、哪些能领」，
    不发任何写请求（脚本默认就是 dry-run，此处不传 `--yes`）。
  · `claim`   —— `--yes --only-claim`：**只把已完成任务的奖励领回来**，
    是幂等的（已领过服务端返回业务错误，视为正常）。不伪造任何行为。
  · `full`    —— `--yes`：点亮 + 领奖。**会伪造活跃上报**（造画布、连发对话、
    批量使用专家等），这是风控最容易识别的模式，上游自己都标注「写操作慎用，
    请确认决策后再跑」。因此**只允许手动触发，且必须显式确认**，不参与定时。

定时只跑 `claim`：它是幂等的领奖，不产生任何伪造行为，风险最低。

安全约束
--------
  · 一次只跑一个（`_lock`）：并发跑等于把同一账号的写请求叠在一起，
    既放大风控信号，也让输出交错难读。
  · 模式走**白名单**，账号标识做字符校验后作为独立 argv 传入（不拼 shell），
    因此不存在命令注入面。
  · 执行前先确认上游目录与脚本存在，**前置拒绝并给出替代做法**，
    而不是跑到一半才失败。
  · 账号目录通过 `WB2A_AUTHS` 显式传给脚本，保证它读的就是本面板管的那批
    auth 文件（脚本自身的回落顺序与本面板的 AUTH_DIR 可能不一致）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from .. import config, db

logger = logging.getLogger('workbuddy.taskrun')

# 运行模式白名单（见模块注释的风险分级）。值 = 传给脚本的参数。
#
# 前三种调的是 `scripts/task_runner.py`（成长任务一体机）；后两种调的是
# `scripts/school_open_day_2026.py`（开学季活动，含幸运大转盘抽奖）。
# 之所以把开学季也收进来：它**不是**成长任务那套接口，但同样是「面板能触发、
# 结果值得留痕」的任务，且它的抽奖中奖信息只打在脚本 stdout 里（容器日志
# 看不到），只能由面板接住后落库（见 `_record_lottery`）。
_MODE_ARGS: dict[str, list[str]] = {
    'preview': [],                       # 默认 dry-run，只读
    'claim': ['--yes', '--only-claim'],  # 幂等领奖
    'full': ['--yes'],                   # 点亮 + 领奖（会伪造上报）
    # 开学季：--run 做任务 + 领奖 + 收尾抽奖（会发写请求）；--lottery-only 只抽奖
    'school': ['--run', '--yes'],
    'school-lottery': ['--lottery-only', '--yes'],
}

# 调 school 脚本的模式前缀（决定用哪个脚本文件）
_SCHOOL_MODES = ('school', 'school-lottery')
_SCHOOL_SCRIPT = 'school_open_day_2026.py'

# 开学季转盘奖品：脚本打印的是**中文标签**（不是 prize_code），这里反查回码。
# 与上游脚本 `LOTTERY_PRIZE_LABELS` 保持一致；脚本新增奖品时这里落不到就按
# 未知奖品记录（label 原样保留），不编造 code。
_LOTTERY_LABELS: dict[str, tuple[str, str]] = {
    '瑞幸咖啡15元券': ('school_voucher_luckin', 'voucher'),
    '肯德基OK餐券': ('school_voucher_kfc_ok', 'voucher'),
    '肯德基冰淇淋券': ('school_voucher_kfc_ice', 'voucher'),
    '酷狗会员月卡券': ('school_voucher_kugou', 'voucher'),
}
# 中奖行：`[school2026] <uid8> draw -> <label>（balance <n>）`
_DRAW_LINE = re.compile(
    r'\[school2026\]\s+(\S+)\s+draw\s+->\s+(.+?)（balance\s+\d+）')
# 积分奖标签形如 `6积分（+6 Credit）`，从中取第一个整数
_CREDIT_LABEL = re.compile(r'^(\d+)\s*积分')


# 单次运行的**空闲**上限（秒）：多久没有新输出就判定卡死并终止。
#
# 为什么按「空闲」而不是「总时长」：脚本的耗时**随账号数线性增长**——它每做一个
# 写动作至少间隔 1s（脚本自身约束），全量任务一轮约 40 个动作，于是
#   6 个账号 ≈ 4 分钟、54 个账号 ≈ 36 分钟（还只是下限）。
# 用固定总时长（初版写的是 30 分钟）会把「大池子的正常全量」**中途杀掉**，
# 比不设还糟：账号做了一半、状态半途而废，用户还得重跑。
# 改按空闲判定：只要脚本还在产出（每个动作都会打一行），就说明它活着；
# 真正卡死时它会静默，这时才终止。取值要明显大于「单步最长耗时」——
# 单个动作含网络往返与 1s 间隔，实测远低于 2 分钟，取 5 分钟留足余量。
IDLE_TIMEOUT_SECONDS = 300

# 兜底总上限（秒）：防止「每 4 分钟吐一行」的诡异形态无限跑下去。
# 30 个账号以内都够用；更大的池子建议分批跑（界面按账号逐个触发）。
MAX_TOTAL_SECONDS = 4 * 3600

# 输出缓冲上限：脚本会为每个动作打一行，全量批量可能几千行。只保留尾部，
# 避免长时间运行把内存吃满（界面也只需要看最近的）。
_MAX_LOG_LINES = 400

# 账号标识：uid 前缀（16 进制）或字面量 ALL。做白名单式字符校验，
# 因为它会作为独立 argv 传进子进程。
_ACCOUNT_RE = re.compile(r'^[0-9a-zA-Z_-]{1,64}$')

_state: dict = {
    'running': False,
    'mode': '',
    'target': '',
    'started_at': 0,
    'finished_at': 0,
    'exit_code': None,
    'timed_out': False,
    'error': '',
    'lines': [],
    # 本次运行从脚本 stdout 里抓到的中奖记录（见 `_capture_lottery`）。
    # 单独存一份而不是事后从 lines 里捞：lines 只保留尾部 400 行，全量任务
    # 的抽奖行可能已被挤掉，而中奖记录是要落库的事实，不能丢。
    'prizes': [],
}
_task: asyncio.Task | None = None


def _host_script() -> Path | None:
    """宿主机挂载目录里的脚本（`<上游目录>/scripts/task_runner.py`），没有则 None。

    这条路径适用于**源码部署 / 完整挂载**的形态：上游目录是宿主机上的 git 仓库，
    scripts/ 就在那里。
    """
    from . import updater  # 复用既有的上游目录推断，避免两处口径不一
    p = updater._upstream_dir() / 'scripts' / 'task_runner.py'
    return p if p.is_file() else None


def _host_school_script() -> Path | None:
    """宿主机挂载目录里的开学季脚本，没有则 None（与 `_host_script` 同口径）。"""
    from . import updater
    p = updater._upstream_dir() / 'scripts' / _SCHOOL_SCRIPT
    return p if p.is_file() else None


def _extract_dir() -> Path:
    """从上游容器镜像提取出的脚本的存放目录。"""
    return config.DATA_DIR / 'upstream-scripts'


# 提取失败的冷却时间（秒）。
#
# 为什么需要：界面的「一键执行」面板闲着时每 15 秒轮询一次 `status()`，而
# `status()` 会调 `available()` → `_script_path()` → 可能触发提取。脚本缺失时
# （例如用户没挂 docker.sock）不设冷却就会每 15 秒 fork 一次 `docker cp` 并失败，
# 日志被刷满、CPU 白耗，而用户看到的东西没有任何变化。
# 取 60 秒：既不让人等太久，也不会把面板开着就变成后台进程生成器。
_EXTRACT_COOLDOWN_SECONDS = 60
_last_extract_failure: dict = {'at': 0.0, 'reason': ''}

# 镜像 ID 的缓存（见 `_upstream_image_id`）。为什么必须有：`available()` 每次都会
# 走到镜像指纹比较，而它是被**界面每 15 秒轮询**的路径；不缓存就等于每 15 秒
# fork 一次 `docker inspect`。
#
# TTL 取 5 秒：远小于轮询间隔（不会变成轮询放大器），又远大于一次检查的耗时，
# 于是「上游刚更新完」最多 5 秒后就被发现，用户感知不到延迟。
_IMAGE_ID_TTL_SECONDS = 5
_image_id_cache: dict = {'at': 0.0, 'id': ''}


def _cache_is_fresh() -> bool:
    """提取出来的脚本是否还对应**当前**上游容器镜像。

    用镜像 ID 作指纹：上游更新（`docker compose up -d --build` 重建镜像）后 ID
    会变，我们就重新提取。这也顺带保证了版本一致 —— 脚本与容器里的上游二进制
    始终来自同一份代码，不会出现「脚本比上游新/旧」的漂移。
    """
    stamp = _extract_dir() / '.image-id'
    try:
        return stamp.read_text(encoding='utf-8').strip() == _upstream_image_id()
    except OSError:
        return False


def _upstream_image_id() -> str:
    """上游容器的镜像 ID；取不到返回空串（视为「无法比较」，走重取）。

    这是**阻塞**调用（`docker inspect`，最多 15 秒），且被 `available()` 间接
    调用 —— 而 `available()` 的调用方里有事件循环内的路径（定时领奖循环、
    启动任务的 async 接口）。因此：

      * 结果带 5 秒缓存：`available()` 每次都会走到这里（路径解析 → 缓存新鲜度
        判断），不缓存就变成「每个请求 fork 一次 docker inspect」；
      * 事件循环里的调用方请用 `available_async()`，它会把这个函数丢进线程池，
        否则一次 docker 卡顿就会冻住整个服务（含对外网关）。
    """
    import subprocess

    now = time.time()
    cached_at, cached_id = _image_id_cache['at'], _image_id_cache['id']
    if now - cached_at < _IMAGE_ID_TTL_SECONDS:
        return cached_id

    value = ''
    try:
        proc = subprocess.run(
            ['docker', 'inspect', '--format', '{{.Image}}', config.WB2API_CONTAINER],
            capture_output=True, text=True, timeout=15,
        )
        value = (proc.stdout or '').strip() if proc.returncode == 0 else ''
    except Exception:  # noqa: BLE001
        value = ''
    _image_id_cache.update({'at': now, 'id': value})
    return value


def extract_scripts(rep: logging.Logger | None = None) -> tuple[bool, str]:
    """把上游容器里的 `scripts/` 提取到本地，供本面板调用。

    为什么要提取而不是 `docker exec` 每次跑：
      · 脚本会 glob `auths/` 并读写文件、还会访问网络；在容器里跑要处理它的
        工作目录、uid、以及把 auths 暴露进去 —— 每个环节都是新的失败面；
      · 提取一次后就是普通文件，与宿主机部署完全同一条执行路径，调试、日志、
        超时控制都复用同一套；
      · 用**镜像 ID** 作指纹，上游更新后自动重取，不存在版本漂移。

    提取的是**整组**脚本而不是单个 task_runner.py：它 `import task_common` 与
    `school_open_day_2026`（后者又 import task_common），少一个就会在运行时
    ImportError。拷贝用 `docker cp <容器>:<目录>/.` 保留整组。

    失败后 60 秒内不再重试（见 `_EXTRACT_COOLDOWN_SECONDS`）：调用方是轮询路径，
    没冷却会变成每 15 秒 fork 一次必然失败的 docker。
    """
    import subprocess

    now = time.time()
    if now - _last_extract_failure['at'] < _EXTRACT_COOLDOWN_SECONDS:
        return False, _last_extract_failure['reason']

    container = config.WB2API_CONTAINER
    dest = _extract_dir()

    def _fail(reason: str) -> tuple[bool, str]:
        _last_extract_failure.update({'at': time.time(), 'reason': reason})
        return False, reason

    try:
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ['docker', 'cp', f'{container}:/app/scripts/.', str(dest)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return _fail('未找到 docker 命令，无法从上游容器提取脚本')
    except Exception as exc:  # noqa: BLE001
        return _fail(f'从上游容器提取脚本失败：{exc}')

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or '').strip()
        return _fail(f'从上游容器提取脚本失败（{container}）：{err}')

    if not (dest / 'task_runner.py').is_file():
        return _fail('提取后仍未找到 task_runner.py（容器内 /app/scripts/ 是否存在？）')

    # 记指纹：写失败不影响本次可用（下次会重取，只是多花一次 docker cp）
    try:
        (dest / '.image-id').write_text(_upstream_image_id(), encoding='utf-8')
    except OSError:
        pass
    _last_extract_failure.update({'at': 0.0, 'reason': ''})
    if rep:
        rep.info('已从上游容器提取任务脚本到 %s', dest)
    return True, str(dest / 'task_runner.py')


def _script_path() -> Path:
    """定位 task_runner.py：宿主机挂载目录优先，其次容器提取。

    两种部署形态各对应一条：
      · 源码 / 完整挂载（`WB_UPSTREAM_DIR` 是宿主机上的仓库）→ 直接用挂载目录，
        这是最省事的，也不需要 docker；
      · **官方镜像部署**（issue #29）→ 脚本被 COPY 进镜像的 `/app/scripts/`，
        宿主机那个目录里**根本没有 scripts/**（你只挂了 config.json 与 auths），
        所以回落去容器里提取。

    注意不要为了「让第一条路径有东西」而让用户手动拷贝脚本：那样每次上游更新
    都要重来一次，迟早版本对不上（issue 里正是这么抱怨的）。
    """
    return _locate_script(school=False)


def _school_script_path() -> Path:
    """定位 school_open_day_2026.py（开学季 + 幸运大转盘）。

    与 `_script_path` 同一套定位逻辑（宿主机挂载优先 → 容器提取回落），只是
    文件名不同。单独一个函数而不是给 `_script_path` 加参数，是为了让既有调用
    点与测试替身（mock 的 0 参 lambda）逐字不变。
    """
    return _locate_script(school=True)


def _locate_script(*, school: bool) -> Path:
    name = _SCHOOL_SCRIPT if school else 'task_runner.py'
    host = _host_school_script() if school else _host_script()
    if host is not None:
        return host
    extracted = _extract_dir() / name
    # `_cache_is_fresh()` 以整组脚本的镜像指纹为准，两种脚本共用同一份提取产物
    if extracted.is_file() and _cache_is_fresh():
        return extracted
    ok, _ = extract_scripts()
    if ok:
        return extracted
    return extracted  # 返回该路径，由 available() 给出可读的失败说明


def _python() -> str:
    """解释器：脚本依赖第三方库为零（纯标准库），用当前解释器最稳。"""
    import sys
    return sys.executable or 'python3'


def available(school: bool = False) -> tuple[bool, str]:
    """能否执行（上游目录与脚本是否到位）。返回 (可用, 说明)。

    单独抽出来是为了让接口能在**动手前**就拒绝，并把原因说清楚 ——
    用户看到「跑不了」时最想知道的是「为什么、我该怎么办」。

    **这是阻塞调用**（可能 fork `docker inspect` / `docker cp`）。在事件循环里
    调用请改用 `available_async()`，否则一次 docker 卡顿会冻住整个服务。
    """
    script = _school_script_path() if school else _script_path()
    if not script.is_file():
        which = _SCHOOL_SCRIPT if school else 'task_runner.py'
        return False, (
            f'未找到上游任务脚本（{script}）。'
            f'该功能调用的是上游 workbuddy2api 自带的 scripts/{which}。\n'
            '两种部署形态各有一种修法：\n'
            '  · 官方镜像部署：本面板会尝试用 `docker cp` 从上游容器里提取脚本，'
            '但它需要能访问 docker（挂载 /var/run/docker.sock，且容器名与 '
            'WB2API_CONTAINER 一致）。请检查这两项，然后点「预览」重试。\n'
            '  · 源码部署：请确认上游目录（WB_UPSTREAM_DIR）已挂载且版本较新，'
            '可到「设置 → 系统更新」更新上游。'
        )
    if not config.AUTH_DIR.is_dir():
        return False, (
            f'账号目录不存在（{config.AUTH_DIR}），无法确定对哪些账号执行。'
            '请检查 WB_AUTH_DIR 配置。'
        )
    return True, ''


async def available_async() -> tuple[bool, str]:
    """`available()` 的**线程池**版本，供事件循环内的调用方使用。

    为什么需要：`available()` 可能 fork `docker inspect`（≤15s）与 `docker cp`
    （≤60s）。在事件循环里直接调它，一次 docker 卡顿就会冻住**整个服务** ——
    包括对外的 `/v1/*` 网关（那才是用户真正在用的东西）。而它有两个调用方
    正好在事件循环里：定时领奖循环、启动任务的 async 接口。

    与 `tasklog` 读容器日志同一个做法（那里也是 `asyncio.to_thread`）。
    """
    return await asyncio.to_thread(available)


def status() -> dict:
    """当前/上次运行状态（供界面轮询）。

    同步版本：路由层是 `def`（FastAPI 会丢进线程池），所以这里阻塞是安全的。
    事件循环里请用 `status_async()`。
    """
    snap = dict(_state)
    snap['lines'] = list(_state['lines'])
    snap['prizes'] = list(_state['prizes'])
    snap['available'], snap['unavailable_reason'] = available()
    # 开学季走的是另一个脚本，可用性单独探测（它可能因上游版本较旧而不存在，
    # 与成长任务脚本的可用性互不影响）。
    snap['school_available'], snap['school_unavailable_reason'] = available(school=True)
    snaps = db.get_setting('task_claim_schedule') or {}
    snap['schedule'] = snaps if isinstance(snaps, dict) else {}
    return snap


def build_command(mode: str, target: str) -> list[str]:
    """构造 argv（不经过 shell，因此不做任何转义）。"""
    if mode not in _MODE_ARGS:
        raise ValueError(f'不支持的模式：{mode}')
    if not _ACCOUNT_RE.match(target or ''):
        raise ValueError('账号标识不合法（只允许 uid 前缀或 ALL）')
    # `-u` 必须加：非交互（管道）时 Python 的 stdout 是**块缓冲**，攒满 8KB 才刷。
    # 脚本每行约 80 字节，于是一次全量要跑满约 100 行才会吐出第一批输出——
    # 用户看到的就是「点了做任务，卡半天没有任何输出，然后突然冒出一大段」
    # （线上实测）。加 -u 后每行实时可见，面板才真的能当进度看。
    script = _school_script_path() if mode in _SCHOOL_MODES else _script_path()
    # 通过面板自带的兼容 runner 启动：它不改上游协议，只对 list_tasks 的
    # 临时网络失败做有限重试，并在重试耗尽时跳过当前任务而不是让整轮崩溃。
    runner = Path(__file__).with_name('taskscript.py')
    return [_python(), '-u', str(runner), str(script), target, *_MODE_ARGS[mode]]


def _prepare(mode: str, target: str) -> tuple[bool, object]:
    """启动前的**阻塞**准备：校验可用性 + 构造 argv。

    返回 (ok, payload)：ok 为 False 时 payload 是给用户的原因说明，否则是 argv。

    单独拆出来是这次修 bug 的关键（issue #31）：启动这件事有两半，性质完全不同——
      · **这一半是阻塞的**：`available()` 可能 fork `docker inspect` / `docker cp`
        （最长 60 秒），`build_command()` 要解析脚本路径（可能触发同一件事）；
      · 另一半**必须在事件循环线程里做**：`loop.create_task()` 调度后台任务。

    早先把两半写在同一个 `start()` 里，再让 `start_async()` 把整个函数丢进线程池
    → 线程池的工作线程没有运行中的事件循环，`get_running_loop()` 必然抛
    RuntimeError，于是「一键执行」100% 报「当前环境没有事件循环，无法后台执行」。
    """
    ok, why = available(mode in _SCHOOL_MODES)
    if not ok:
        return False, why
    if _state['running']:
        return False, f'已有任务正在执行（{_state["mode"]} / {_state["target"]}），请等它结束'
    try:
        argv = build_command(mode, target)
    except ValueError as exc:
        return False, str(exc)
    return True, argv


def _launch(argv: list[str], mode: str, target: str, loop) -> tuple[bool, str]:
    """登记状态并调度后台任务。**必须在事件循环线程里调用**（要 create_task）。"""
    global _task
    _state.update({
        'running': True, 'mode': mode, 'target': target,
        'started_at': int(time.time()), 'finished_at': 0,
        'exit_code': None, 'timed_out': False, 'error': '', 'lines': [],
        'prizes': [],
    })
    _task = loop.create_task(_run(argv, mode, target))
    return True, f'已开始执行（{mode}）'


async def start_async(mode: str, target: str = 'ALL') -> tuple[bool, str]:
    """启动一次执行（**不阻塞事件循环**的版本）。

    定时领奖循环与启动任务的 async 接口都走这里：把阻塞的准备丢进线程池，
    再回到事件循环线程里调度任务。两半的运行位置不能混（见 `_prepare`）。
    """
    ok, payload = await asyncio.to_thread(_prepare, mode, target)
    if not ok:
        return False, payload  # type: ignore[return-value]
    return _launch(list(payload), mode, target, asyncio.get_running_loop())  # type: ignore[arg-type]


def start(mode: str, target: str = 'ALL') -> tuple[bool, str]:
    """启动一次执行（同步版）。返回 (是否已启动, 说明)。

    要求**当前线程有运行中的事件循环**（要 create_task），且会阻塞它 ——
    所以事件循环里的调用方请用 `start_async()`。保留同步版是为了命令行/测试
    这类「本来就在 loop 线程里、且不在意短暂阻塞」的场景。
    """
    ok, payload = _prepare(mode, target)
    if not ok:
        return False, payload  # type: ignore[return-value]
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False, '当前环境没有事件循环，无法后台执行'
    return _launch(list(payload), mode, target, loop)  # type: ignore[arg-type]


async def _run(argv: list[str], mode: str, target: str) -> None:
    """跑子进程并把输出累积到状态里（尾部保留）。"""
    env = dict(os_environ())
    # 显式指定账号目录：脚本自身的回落顺序（WB2A_AUTHS > 仓库内 auths/ >
    # /root/...）不一定指向本面板管的那批文件，传错会「跑了个寂寞」还看不出来。
    env['WB2A_AUTHS'] = str(config.AUTH_DIR)
    # 与 argv 里的 `-u` 双保险：两者都是「不缓冲」的表达，任一被忽略时另一个兜住
    # （脚本将来若自己拉起子进程，环境变量也能继承下去）。
    env['PYTHONUNBUFFERED'] = '1'
    # 上游目录作为 cwd：脚本以 __file__ 自定位，但仍按上游惯例从仓库根运行。
    # 丢进线程池：`_script_path()` 在脚本缺失时会触发 docker 提取（阻塞），
    # 而本函数跑在事件循环里。
    script_path = await asyncio.to_thread(
        _school_script_path if mode in _SCHOOL_MODES else _script_path)
    cwd = str(script_path.parent.parent)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        # 按**空闲**判定卡死，而不是总时长（理由见 IDLE_TIMEOUT_SECONDS 注释）：
        # 每读到一行就把「最后一次活动」往后推，静默超过阈值才判定卡死。
        last_seen = time.time()
        started = time.time()

        async def pump_with_deadline() -> None:
            nonlocal last_seen
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
                except asyncio.TimeoutError:
                    idle = time.time() - last_seen
                    if idle > IDLE_TIMEOUT_SECONDS:
                        raise _Stalled(idle)
                    if time.time() - started > MAX_TOTAL_SECONDS:
                        raise _Stalled(time.time() - started, total=True)
                    continue
                if not raw:
                    return
                line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                if line:
                    _append(line)
                    # 开学季抽奖的中奖行只出现在脚本 stdout 里（容器日志看不到），
                    # 边读边抓，落库时才不会因 lines 只留尾部而丢记录。
                    _capture_lottery(line)
                    last_seen = time.time()

        try:
            await pump_with_deadline()
        except _Stalled as exc:
            _state['timed_out'] = True
            if getattr(exc, 'total', False):
                _append(f'!! 运行超过 {MAX_TOTAL_SECONDS // 3600} 小时上限，已终止')
            else:
                _append(f'!! 已 {int(exc.args[0])}s 无输出，判定卡死并终止')
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        code = await proc.wait()
        _state['exit_code'] = code
    except FileNotFoundError as exc:
        _state['error'] = f'无法启动脚本：{exc}'
        logger.warning('任务脚本启动失败: %s', exc)
    except Exception as exc:  # noqa: BLE001
        _state['error'] = str(exc)[:300]
        logger.warning('任务执行异常: %s', exc)
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        _state['running'] = False
        _state['finished_at'] = int(time.time())
        _record_lottery(mode, target)
        _record_history(mode, target)


class _Stalled(Exception):
    """内部信号：判定子进程卡死（空闲超阈值 / 超过兜底总上限）。"""


def _append(line: str) -> None:
    lines = _state['lines']
    lines.append(line[:400])
    if len(lines) > _MAX_LOG_LINES:
        # 丢最旧的，保留尾部（界面只展示最近的）
        del lines[:len(lines) - _MAX_LOG_LINES]


def _capture_lottery(line: str) -> None:
    """从一行脚本输出里抓出中奖记录，追加到 `_state['prizes']`。

    脚本的中奖行形如：

        [school2026] 2fb53fe4 draw -> 瑞幸咖啡15元券（balance 0）
        [school2026] 2fb53fe4 draw -> 6积分（+6 Credit）（balance 3）

    只认 `draw -> <label>（balance <n>）` 这一种形态：它是**真正中奖**的行；
    脚本里「无一抽出奖」「未中奖，停」等行不匹配，不会被误记成中奖。
    """
    m = _DRAW_LINE.search(line or '')
    if not m:
        return
    uid8 = m.group(1).strip()
    label = m.group(2).strip()
    credit = 0
    m_credit = _CREDIT_LABEL.match(label)
    if m_credit:
        prize_type = 'credit'
        credit = int(m_credit.group(1))
        code = ''          # 积分奖没有 voucher code；label 本身已说明数量
    else:
        hit = _LOTTERY_LABELS.get(label)
        if hit:
            code, prize_type = hit
        else:
            # 未知奖码：如实记录 label，不编造 code（与脚本 lottery_prize_text 同口径）
            code, prize_type = '', 'other'
    _state['prizes'].append({
        'uid': uid8,
        'label': label,
        'prize_code': code,
        'prize_type': prize_type,
        'credit': credit,
    })


def _record_lottery(mode: str, target: str) -> None:
    """把本次运行抓到的中奖记录落库（幂等：dedup_key 重复的忽略）。

    只在开学季模式（school / school-lottery）下写——其它模式脚本不抽奖，
    `_state['prizes']` 必为空，写也是白写。

    dedup_key = `school:<结束时间戳>:<账号>:<奖码或标签>:<该账号第几抽>`：
    同一次运行重复解析（前端轮询状态多次读到同一份 prizes）不会重复入库；
    而不同轮次的同一奖码会各自留痕（那是真的又中了一次）。
    """
    if mode not in _SCHOOL_MODES or not _state['prizes']:
        return
    finished = _state['finished_at'] or int(time.time())
    counts: dict[str, int] = {}
    entries: list[dict] = []
    for p in _state['prizes']:
        uid = str(p.get('uid') or '')
        key_id = p.get('prize_code') or p.get('label') or '?'
        counts[key_id] = counts.get(key_id, 0) + 1
        entries.append({
            'ts': finished,
            'uid': uid,
            'nickname': '',   # 昵称由前端/接口按 uid 解析（与 task_logs 同口径）
            'prize_code': p.get('prize_code') or '',
            'label': p.get('label') or '',
            'prize_type': p.get('prize_type') or 'other',
            'credit': int(p.get('credit') or 0),
            'source': 'school',
            'status': 'pending',
            'dedup_key': f'school:{finished}:{uid}:{key_id}:{counts[key_id]}',
        })
    try:
        added = db.add_lottery_prizes(entries)
        if added:
            logger.info('开学季抽奖记录已入库 %d 条', added)
    except Exception as exc:  # noqa: BLE001
        # 落库失败不能影响脚本本身的执行结果（用户已跑完任务，记录丢了可重跑）
        logger.warning('写入抽奖记录失败（不影响执行）: %s', exc)


def _record_history(mode: str, target: str) -> None:
    """把这次执行的结果写进「任务记录」，让它在历史里能查到。

    只在**真正可能产生写入**的模式下记录（claim / full / school /
    school-lottery），且只记一行结果 —— 逐行上报日志交给采集器读容器日志，
    这里记的是「面板发起的这次操作」。
    """
    if mode not in ('claim', 'full', 'school', 'school-lottery'):
        return
    summary = _summarize()
    label = {
        'claim': '一键领奖', 'full': '一键做任务',
        'school': '开学季任务', 'school-lottery': '开学季抽奖',
    }[mode]
    level = 'ok'
    if _state['error'] or _state['timed_out'] or (_state['exit_code'] not in (0, None)):
        level = 'fail'
    try:
        db.add_task_logs([{
            'ts': _state['finished_at'] or int(time.time()),
            'uid': '' if target == 'ALL' else target,
            'kind': 'taskrun',
            'level': level,
            'credits': 0,
            'message': f'{label}（{target}）{summary}',
            # dedup_key 必须唯一：同一次执行只记一条，用结束时间戳即可
            'dedup_key': f'taskrun:{mode}:{target}:{_state["finished_at"]}',
        }])
    except Exception as exc:  # noqa: BLE001
        logger.warning('写入任务执行记录失败（不影响执行）: %s', exc)


def _summarize() -> str:
    """从脚本输出里挑出**结果汇总**行。

    注意别抓错行：脚本开头会打一行 `mode=DRY-RUN accounts=[...]`，那只是**运行
    参数**（不是结果）；真正的结果在结尾的 `task_runner done: accounts=1 ok=3 …`
    （开学季脚本则是 `school2026 done: accounts=… ok=…`）。
    早先按 `mode=` 匹配，抓到的是参数行 —— 记录进历史的「结果」就变成了
    「mode=DRY-RUN …」，等于没记结果（实测发现）。
    """
    for line in reversed(_state['lines']):
        if 'done:' in line or 'task_runner done' in line:
            return line[:200]
    # 没有结果行（例如启动就失败）：用错误/退出码兜底
    if _state['error']:
        return f'失败：{_state["error"][:140]}'
    if _state['timed_out']:
        return '超时终止'
    code = _state['exit_code']
    return '完成' if code == 0 else f'退出码 {code}'


def os_environ() -> dict:
    """单独抽成函数便于测试替换（测试里不需要真实环境变量）。"""
    import os
    return dict(os.environ)


async def stop() -> bool:
    """终止正在跑的进程（界面上的「停止」）。"""
    global _task
    if not _state['running'] or _task is None or _task.done():
        return False
    _task.cancel()
    _state['running'] = False
    _state['finished_at'] = int(time.time())
    _append('!! 已手动停止')
    return True


# ── 定时领奖（仅 claim 模式）────────────────────────────────
# 只定时跑**幂等领奖**：它不伪造任何行为，只是把账号已完成任务的奖励领回来。
# 「点亮」（full 模式）会产生伪造活跃上报，明确排除在定时之外。

_CLAIM_TICK_SECONDS = 60
_last_claim_day: str = ''
_claim_task: asyncio.Task | None = None


def get_schedule() -> dict:
    """定时领奖的配置（enabled + 整点数组）。"""
    raw = db.get_setting('task_claim_schedule')
    if not isinstance(raw, dict):
        raw = {}
    hours = raw.get('hours')
    if not isinstance(hours, list):
        hours = [10]
    hours = sorted({int(h) for h in hours
                    if isinstance(h, int) and not isinstance(h, bool) and 0 <= h <= 23})
    return {'enabled': bool(raw.get('enabled')), 'hours': hours or [10]}


def set_schedule(enabled: bool, hours: list[int]) -> dict:
    """保存定时领奖配置（时间点做合法性校验）。"""
    if not isinstance(enabled, bool):
        raise ValueError('enabled 必须是布尔值')
    if not isinstance(hours, list) or not hours:
        raise ValueError('hours 必须是非空数组')
    clean: list[int] = []
    for h in hours:
        if isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23:
            raise ValueError('hours 必须是 0-23 的整点')
        if h not in clean:
            clean.append(h)
    cfg = {'enabled': enabled, 'hours': sorted(clean)}
    db.set_setting('task_claim_schedule', cfg)
    return cfg


async def _claim_loop() -> None:
    """到点跑一次 claim（每天每个整点最多一次）。"""
    global _last_claim_day
    while True:
        try:
            cfg = get_schedule()
            if cfg['enabled']:
                now = time.localtime()
                # 本地时区与用户一致（面板按用户所在时区显示时间）
                if now.tm_hour in cfg['hours'] and now.tm_min < 2:
                    stamp = f'{now.tm_year}-{now.tm_mon}-{now.tm_mday}-{now.tm_hour}'
                    if stamp != _last_claim_day:
                        _last_claim_day = stamp
                        if not _state['running']:
                            logger.info('定时领奖触发（%s 点档）', now.tm_hour)
                            # 必须 await 线程池版本：start() 里的 available()
                            # 可能 fork docker（脚本缺失时），在事件循环里同步跑
                            # 会冻住整个服务连同对外网关。
                            await start_async('claim', 'ALL')
        except Exception as exc:  # noqa: BLE001
            logger.warning('定时领奖检查失败: %s', exc)
        await asyncio.sleep(_CLAIM_TICK_SECONDS)


def start_scheduler() -> bool:
    """启动定时领奖检查（幂等）。"""
    global _claim_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _claim_task is None or _claim_task.done():
        _claim_task = loop.create_task(_claim_loop())
    return True


def stop_scheduler() -> None:
    global _claim_task
    if _claim_task and not _claim_task.done():
        _claim_task.cancel()
