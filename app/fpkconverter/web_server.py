#!/usr/bin/env python3
import os, sys, traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOT_LOG = os.path.join(SCRIPT_DIR, 'boot.log')
try:
    with open(BOOT_LOG, 'w') as f:
        f.write(f'BOOTED: {os.path.abspath(__file__)}\n')
        f.write(f'PID: {os.getpid()}\n')
        f.write(f'Python: {sys.version}\n')
except: pass

CRASH_LOG = os.path.join(SCRIPT_DIR, 'crash.log')
def _global_excepthook(etype, value, tb):
    try:
        with open(CRASH_LOG, 'w') as f:
            traceback.print_exception(etype, value, tb, file=f)
    except: pass
    sys.__excepthook__(etype, value, tb)
sys.excepthook = _global_excepthook

PKG_DIR    = os.path.join(SCRIPT_DIR, 'packages')
VAR_DIR    = os.environ.get('TRIM_PKGVAR') or os.path.join(SCRIPT_DIR, 'data')
os.makedirs(VAR_DIR, exist_ok=True)

if os.path.isdir(PKG_DIR) and PKG_DIR not in sys.path:
    sys.path.insert(0, PKG_DIR)

DB_PATH     = os.path.join(VAR_DIR, 'fpk_converter.db')
CONFIG_PATH = os.path.join(VAR_DIR, 'config.json')
CONV_LOG    = os.path.join(VAR_DIR, 'converter.log')
# 由 cmd/config_callback 把 TRIM_DATA_ACCESSIBLE_PATHS 写入此文件，
# 让 web 进程在用户变更授权后无需重启即可感知。
ACCESSIBLE_PATHS_FILE = os.path.join(VAR_DIR, 'accessible_paths')
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

import subprocess, sqlite3, time, json, threading
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

@app.after_request
def add_headers(r):
    r.headers['X-Content-Type-Options'] = 'nosniff'
    return r

ALLOWED = {'monitor_dir','monitor_dirs','crf','codec','container','preset','threads','use_gpu','resize_mode','custom_resize','enabled'}
CODECS = ('libx264','libx265')
CONTAINERS = ('mp4','mkv')
PRESETS = ('ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow')
RESIZE_MODES = ('keep','1080p','custom')
DEFAULT = {'monitor_dir':'','monitor_dirs':[],'crf':23,'codec':'libx264','container':'mp4',
           'preset':'medium','threads':1,'use_gpu':True,'resize_mode':'1080p','custom_resize':'',
           'enabled':False}
MIN_CRF = 18
MAX_CRF = 32
MAX_THREADS = 4
# 严格按照飞牛官方规范：应用只能浏览
# 1) TRIM_DATA_ACCESSIBLE_PATHS 中由用户在"应用设置→授权目录"显式授权的目录
# 2) TRIM_DATA_SHARE_PATHS 中由 config/resource:data-share 声明的共享目录
# 这两个变量由 fnOS 在脚本/服务启动时注入；变更时通过 cmd/config_callback 通知。
# 应用从不主动扫描 /、/vol*、/mnt 等系统/挂载目录。
BLOCKED_PATH_PREFIXES = (
    '/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/root',
    '/run', '/sbin', '/sys', '/usr', '/var'
)
cfg = dict(DEFAULT)
proc = None
conv_log_file = None
last_error = ''
proc_started_at = None
proc_lock = threading.Lock()

def _rotate_log_if_needed(path, max_bytes=None, backup_count=None):
    max_bytes = LOG_MAX_BYTES if max_bytes is None else max_bytes
    backup_count = LOG_BACKUP_COUNT if backup_count is None else backup_count
    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) <= max_bytes:
            return False
        for idx in range(backup_count, 0, -1):
            src = f'{path}.{idx}'
            dst = f'{path}.{idx + 1}'
            if os.path.exists(src):
                if idx >= backup_count:
                    os.remove(src)
                else:
                    os.replace(src, dst)
        os.replace(path, f'{path}.1')
        return True
    except Exception as e:
        try:
            with open(path, 'a') as f:
                f.write(f'\n日志轮转失败: {e}\n')
        except Exception:
            pass
        return False

def _normalize_monitor_dirs(cfg_data):
    """将配置归一化：monitor_dirs 为去重、保留顺序的授权目录列表；
    monitor_dir 始终等于列表第一个（方便旧版单值读取）。"""
    dirs = cfg_data.get('monitor_dirs')
    if not isinstance(dirs, list):
        # 旧版只有 monitor_dir：迁移为单元素列表
        single = str(cfg_data.get('monitor_dir') or '').strip()
        dirs = [single] if single else []
    seen, ordered = set(), []
    for d in dirs:
        d = str(d or '').strip().rstrip('/')
        if d and d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered

def _sync_monitor_aliases():
    """保持 monitor_dir 与 monitor_dirs 同步，返回首个目录。"""
    dirs = _normalize_monitor_dirs(cfg)
    cfg['monitor_dirs'] = dirs
    cfg['monitor_dir'] = dirs[0] if dirs else ''
    return cfg['monitor_dir']

def load_cfg():
    global cfg
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f: d = json.load(f)
            if isinstance(d, dict):
                cfg = {**DEFAULT, **{k:v for k,v in d.items() if k in ALLOWED}}
                cfg['monitor_dirs'] = _normalize_monitor_dirs({**d, **cfg})
                cfg['monitor_dir'] = cfg['monitor_dirs'][0] if cfg['monitor_dirs'] else ''
        except: pass

def save_cfg():
    try:
        _sync_monitor_aliases()
        t = CONFIG_PATH + '.tmp'
        with open(t,'w') as f: json.dump(cfg, f, indent=2)
        os.replace(t, CONFIG_PATH)
    except: pass

def init_db():
    try:
        c = sqlite3.connect(DB_PATH)
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS processed_files(
                id INTEGER PRIMARY KEY AUTOINCREMENT, filepath TEXT UNIQUE NOT NULL,
                file_size INTEGER NOT NULL, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success INTEGER DEFAULT 0, saved_size INTEGER DEFAULT 0)''')
            c.commit()
        finally:
            c.close()
    except Exception as e:
        print(f"init_db error: {e}")

def _ensure_problem_schema():
    """确保 problem_files 表存在（接口在转码核心首次运行前也可安全调用）"""
    try:
        c = sqlite3.connect(DB_PATH)
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS problem_files(
                filepath TEXT PRIMARY KEY, reason TEXT DEFAULT '',
                first_failed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_attempt_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                failure_count INTEGER DEFAULT 0, file_size INTEGER DEFAULT 0)''')
            c.commit()
        finally:
            c.close()
    except Exception as e:
        print(f"_ensure_problem_schema error: {e}")

load_cfg(); init_db()

def _normalize_abs_path(path):
    try:
        value = str(path or '').strip()
        if not value or len(value) > 240:
            return None, '路径为空或过长'
        if not value.startswith('/'):
            return None, '路径必须是绝对路径'
        normalized = os.path.normpath(value)
        if normalized != value or '..' in normalized.split(os.sep):
            return None, '路径包含不安全的跳转'
        return normalized, ''
    except Exception:
        return None, '路径格式无效'

def _is_blocked_system_path(path):
    normalized, _ = _normalize_abs_path(path)
    if not normalized:
        return True
    if normalized == '/':
        return True
    return any(normalized == prefix or normalized.startswith(prefix + os.sep)
               for prefix in BLOCKED_PATH_PREFIXES)

def _split_path_list(s):
    """按官方约定（冒号分隔）解析 TRIM_DATA_* 类路径列表。"""
    if not s:
        return []
    result = []
    for raw in str(s).split(':'):
        item = raw.strip()
        if not item or not item.startswith('/'):
            continue
        n, _ = _normalize_abs_path(item)
        if n and not _is_blocked_system_path(n):
            if n not in result:
                result.append(n)
    return result

def _read_accessible_paths_file():
    """从持久化文件读取（cmd/config_callback 写入）。"""
    if not ACCESSIBLE_PATHS_FILE or not os.path.exists(ACCESSIBLE_PATHS_FILE):
        return []
    try:
        with open(ACCESSIBLE_PATHS_FILE) as f:
            return _split_path_list(f.read().replace('\n', ':'))
    except Exception:
        return []

def get_authorized_roots():
    """返回应用当前可访问的根目录列表，严格按官方规范：
    1. TRIM_DATA_ACCESSIBLE_PATHS（用户在"应用设置→授权目录"中授权的）
    2. TRIM_DATA_SHARE_PATHS（config/resource:data-share 声明的）
    3. 持久化文件（cmd/config_callback 写入）作为运行时 env 不更新的回退
    """
    roots = []
    for src in (
        os.environ.get('TRIM_DATA_ACCESSIBLE_PATHS', ''),
        os.environ.get('TRIM_DATA_SHARE_PATHS', ''),
    ):
        for p in _split_path_list(src):
            if p not in roots:
                roots.append(p)
    for p in _read_accessible_paths_file():
        if p not in roots:
            roots.append(p)
    return roots

def _is_under_authorized_root(path):
    """检查 path 是否位于已授权的根目录之下（含相等）。"""
    if not path:
        return False
    for root in get_authorized_roots():
        if path == root or path.startswith(root.rstrip('/') + os.sep):
            return True
    return False

def _validate_user_directory(path, must_exist=False):
    normalized, err = _normalize_abs_path(path)
    if not normalized:
        return None, err
    if _is_blocked_system_path(normalized):
        return None, f'禁止选择系统目录: {normalized}'
    if not _is_under_authorized_root(normalized):
        return None, (f'目录未授权访问: {normalized}\n'
                      f'请前往"应用设置→授权目录"对该目录授予读写权限')
    if must_exist and not os.path.isdir(normalized):
        return None, f'目录不存在: {normalized}'
    return normalized, ''

def _is_running():
    global proc
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:
        return False

def _kill_stale_converter_processes():
    """清理 web 进程重启后遗留的旧转码子进程，避免旧版本继续写日志。"""
    killed = []
    proc_root = '/proc'
    try:
        current_pid = os.getpid()
        for name in os.listdir(proc_root):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == current_pid:
                continue
            cmdline_path = os.path.join(proc_root, name, 'cmdline')
            try:
                with open(cmdline_path, 'rb') as f:
                    raw = f.read()
                if not raw:
                    continue
                cmdline = raw.replace(b'\x00', b' ').decode('utf-8', errors='replace')
                if 'start_converter.py' not in cmdline:
                    continue
                if VAR_DIR not in cmdline and 'fpkconverter' not in cmdline:
                    continue
                try:
                    os.killpg(os.getpgid(pid), 15)
                except Exception:
                    try:
                        os.kill(pid, 15)
                    except Exception:
                        continue
                killed.append(pid)
            except Exception:
                continue
        if killed:
            print(f"已清理遗留旧转码进程: {killed}", flush=True)
    except Exception as e:
        print(f"清理遗留旧转码进程失败: {e}", flush=True)
    return killed

def _tail_lines(path, max_lines=80, max_bytes=65536):
    try:
        if not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read().decode('utf-8', errors='replace')
        return data.splitlines()[-max_lines:]
    except Exception as e:
        return [f'读取日志失败: {e}']

def _parse_process_state(lines):
    state = {'current_file':'', 'current_activity':'', 'last_error':'', 'progress_percent':0}
    error_keywords = ('错误', '失败', '异常', '未找到', 'Traceback', 'PermissionError', 'Error')
    activity_keywords = ('[SERIAL] 开始处理:', '文件已入队，等待文件稳定:', '开始转码:', '视频信息:',
                         'ffmpeg 命令:', 'QSV 转码失败', '转码进度:', '转码完成:', '已替换原文件:')
    for line in lines:
        clean = line.strip()
        if not clean:
            continue
        if '[SERIAL] 开始处理:' in clean:
            state['current_file'] = clean.split('[SERIAL] 开始处理:', 1)[1].strip()
            state['current_activity'] = clean
            continue
        if clean.startswith('开始转码:'):
            rest = clean.split('开始转码:', 1)[1].strip()
            state['current_file'] = rest.split(' (大小:', 1)[0].strip()
            state['current_activity'] = clean
            continue
        if clean.startswith('文件已入队，等待文件稳定:'):
            rest = clean.split('文件已入队，等待文件稳定:', 1)[1].strip()
            state['current_file'] = rest.split('，剩余约', 1)[0].strip()
            state['current_activity'] = clean
            state['progress_percent'] = 0
            continue
        if clean.startswith('转码进度:'):
            state['current_activity'] = clean
            try:
                pct = clean.split('转码进度:', 1)[1].split('%', 1)[0].strip()
                state['progress_percent'] = float(pct)
            except Exception:
                pass
            if '当前文件:' in clean:
                state['current_file'] = clean.split('当前文件:', 1)[1].strip()
            continue
        if any(k in clean for k in error_keywords):
            state['last_error'] = clean
            continue
        if any(k in clean for k in activity_keywords):
            state['current_activity'] = clean
    return state

def _process_snapshot():
    running = _is_running()
    lines = _tail_lines(CONV_LOG)
    parsed = _parse_process_state(lines)
    pid = None
    returncode = None
    with proc_lock:
        if proc is not None:
            pid = getattr(proc, 'pid', None)
            try:
                returncode = proc.poll()
            except Exception:
                returncode = None
    uptime = None
    if proc_started_at and running:
        uptime = max(0, int(time.time() - proc_started_at))
    return {
        'running': running,
        'pid': pid,
        'started_at': proc_started_at,
        'uptime_seconds': uptime,
        'returncode': returncode,
        **parsed
    }, lines

@app.route('/')
def index():
    _sync_monitor_aliases()
    return render_template_string(HTML, config=cfg, var_dir=VAR_DIR, conv_log=CONV_LOG, db_path=DB_PATH)

@app.route('/api/config', methods=['GET','POST'])
def api_cfg():
    if request.method == 'POST':
        d = request.json
        if not isinstance(d, dict): return jsonify({'success':False}), 400
        for k,v in d.items():
            if k not in ALLOWED: continue
            if k == 'monitor_dir':
                vs, err = _validate_user_directory(v, must_exist=False)
                if not vs:
                    return jsonify({'success':False, 'error':err}), 400
                cfg['monitor_dir'] = vs
                # 兼容旧版单值行为：把 monitor_dir 同步进 monitor_dirs
                dirs = list(cfg.get('monitor_dirs') or [])
                if vs not in dirs:
                    dirs.append(vs)
                cfg['monitor_dirs'] = dirs
                _sync_monitor_aliases()
            elif k == 'monitor_dirs':
                # 多目录：逐项校验并过滤无效项
                new_dirs = []
                for item in (v if isinstance(v, list) else []):
                    vs, err = _validate_user_directory(item, must_exist=False)
                    if vs and vs not in new_dirs:
                        new_dirs.append(vs)
                if any(not _validate_user_directory(item, must_exist=False)[0] for item in (v if isinstance(v, list) else [])):
                    return jsonify({'success':False, 'error':'存在未授权或非法的监控目录'}), 400
                cfg['monitor_dirs'] = new_dirs
                _sync_monitor_aliases()
            elif k == 'crf':
                try: cfg[k] = max(MIN_CRF, min(MAX_CRF, int(v)))
                except: pass
            elif k == 'codec' and v in CODECS: cfg[k] = v
            elif k == 'container' and v in CONTAINERS: cfg[k] = v
            elif k == 'preset' and v in PRESETS: cfg[k] = v
            elif k == 'resize_mode' and v in RESIZE_MODES: cfg[k] = v
            elif k == 'custom_resize':
                cfg[k] = str(v or '').strip()
            elif k == 'threads':
                try: cfg[k] = max(1, min(MAX_THREADS, int(v)))
                except: pass
            elif k == 'use_gpu': cfg[k] = bool(v)
        save_cfg()
        _sync_monitor_aliases()
        return jsonify({'success':True, 'config':cfg})
    _sync_monitor_aliases()
    return jsonify(cfg)

@app.route('/api/status')
def api_status():
    global last_error
    process, lines = _process_snapshot()
    display_error = last_error or process.get('last_error', '')
    return jsonify({'running':process['running'], 'config':cfg, 'error':display_error,
                    'process':process, 'recent_log':lines,
                    'authorized_roots': get_authorized_roots()})

TEMP_DIR = os.path.join(VAR_DIR, 'temp')

@app.route('/api/start', methods=['POST'])
def api_start():
    global proc, last_error, conv_log_file, proc_started_at
    # 先在锁外做验证和文件准备
    last_error = ''
    if _is_running():
        return jsonify({'success':False, 'error':'已在运行中'})
    _sync_monitor_aliases()
    dirs = cfg.get('monitor_dirs') or []
    if not dirs:
        last_error = '请至少选择一个监控文件夹'
        return jsonify({'success':False, 'error':last_error})
    # 逐目录校验必须存在
    validated_dirs = []
    for md in dirs:
        md, err = _validate_user_directory(md, must_exist=True)
        if not md:
            last_error = err
            return jsonify({'success':False, 'error':last_error})
        validated_dirs.append(md)
    os.makedirs(TEMP_DIR, exist_ok=True)
    jp = os.path.join(VAR_DIR, 'start_config.json')
    with open(jp,'w') as f: json.dump({
        'db_path':DB_PATH, 'code_dir':SCRIPT_DIR, 'monitor_dirs':validated_dirs,
        'crf':cfg.get('crf',23), 'codec':cfg.get('codec','libx264'),
        'container':cfg.get('container','mp4'), 'preset':cfg.get('preset','medium'),
        'threads':cfg.get('threads',1), 'use_gpu':cfg.get('use_gpu',True),
        'resize_mode':cfg.get('resize_mode','1080p'), 'custom_resize':cfg.get('custom_resize',''),
        'temp_dir': TEMP_DIR, 'max_depth': 5}, f)
    sc = os.path.join(VAR_DIR, 'start_converter.py')
    script_content = '''import sys,os,json,subprocess,traceback,time
sd=os.path.dirname(os.path.abspath(__file__))
print("=== 转码进程启动 ===")
print(f"Python: {sys.executable}")
print(f"PID: {os.getpid()}")
print(f"CWD: {os.getcwd()}")
print(f"PATH: {os.environ.get('PATH', 'N/A')}")
# 检查 ffmpeg
try:
    r=subprocess.run(["ffmpeg","-version"],capture_output=True,text=True,timeout=10)
    print(f"ffmpeg: 可用 (返回码 {r.returncode})")
    if r.stdout:
        first_line=r.stdout.strip().split("\\n")[0]
        print(f"ffmpeg 版本: {first_line}")
except FileNotFoundError:
    print("ffmpeg: 未找到! 请确认已安装")
except Exception as e:
    print(f"ffmpeg 检查失败: {e}")
# 检查 ffprobe
try:
    r=subprocess.run(["ffprobe","-version"],capture_output=True,text=True,timeout=10)
    print("ffprobe: 可用")
except FileNotFoundError:
    print("ffprobe: 未找到!")
except Exception as e:
    print(f"ffprobe 检查失败: {e}")
jv=os.environ.get("TRIM_PKGVAR") or os.path.join(sd,"..","app","fpkconverter","data")
with open(os.path.join(jv,"start_config.json")) as fh:c=json.load(fh)
print(f"配置: {json.dumps(c, indent=2)}")
sys.path.insert(0,c["code_dir"])
pk=os.path.join(c["code_dir"],"packages")
if os.path.isdir(pk):
    sys.path.insert(0,pk)
import fpk_converter
print(f"视频转码核心版本: {fpk_converter.VERSION}")
fpkc=fpk_converter
db=fpkc.Database(c["db_path"])
td=c.get("temp_dir","")
print(f"temp_dir: {td}")
if td:
    os.makedirs(td,exist_ok=True)
    print(f"temp_dir 可写: {os.access(td, os.W_OK)}")
vc=fpkc.VideoConverter(db,c["crf"],c["codec"],c["container"],c["preset"],c["threads"],c["use_gpu"],temp_dir=td if td else None)
# 透传分辨率策略与自定义 ffmpeg 参数到转码核心
vc.resize_mode=c.get("resize_mode","1080p")
vc.custom_resize=c.get("custom_resize","")
print(f"编码器: {vc.codec}, GPU: {vc.use_gpu}, 分辨率策略: {vc.resize_mode}, 自定义: {vc.custom_resize!r}")
dirs=c.get("monitor_dirs") or ([c["monitor_dir"]] if c.get("monitor_dir") else [])
if not dirs:
    print("未配置任何监控目录，退出")
    sys.exit(2)
ok,msg=vc.preflight_check(dirs[0])
print(f"启动前功能自检结果: {msg}")
if not ok:
    print("启动前功能自检失败，停止进入正式转码流程")
    sys.exit(2)
print(f"启动前功能自检通过，进入正式扫描流程，共 {len(dirs)} 个监控目录")
import threading as _th
scanners=[]
def _run_scanner(d):
    fpkc.FolderScanner(d,vc,max_depth=c.get("max_depth",3)).start()
for d in dirs:
    t=_th.Thread(target=_run_scanner,args=(d,),daemon=True)
    t.start()
    scanners.append(t)
# 主线程等待，保持进程存活
try:
    while True:
        time.sleep(5)
except (KeyboardInterrupt, SystemExit):
    pass
'''
    with open(sc, 'w') as f:
        f.write(script_content)
    os.chmod(sc, 0o755)
    # 只在启动进程和更新状态时持锁，缩小锁范围
    with proc_lock:
        if _is_running():
            return jsonify({'success':False, 'error':'已在运行中'})
        if conv_log_file:
            try: conv_log_file.close()
            except: pass
            conv_log_file = None
        _kill_stale_converter_processes()
        _rotate_log_if_needed(CONV_LOG)
        conv_log_file = open(CONV_LOG, 'a')
        # fnOS 中 sys.executable 可能为空，回退到 python3；使用无缓冲模式确保日志实时写入
        py = sys.executable if sys.executable else 'python3'
        env = dict(os.environ)
        env['PYTHONUNBUFFERED'] = '1'
        proc = subprocess.Popen([py, '-u', sc], cwd=VAR_DIR, start_new_session=True,
                                stdout=conv_log_file, stderr=subprocess.STDOUT, env=env)
        proc_started_at = int(time.time())
    # 非阻塞健康检查：3秒后验证
    def _health_check():
        global proc, last_error, conv_log_file, proc_started_at
        time.sleep(3)
        with proc_lock:
            if proc and not _is_running():
                last_error = '转码进程启动后立刻退出，请检查日志'
                if conv_log_file:
                    try: conv_log_file.close()
                    except: pass
                    conv_log_file = None
                proc = None
                proc_started_at = None
    threading.Thread(target=_health_check, daemon=True).start()
    cfg['enabled'] = True; save_cfg()
    return jsonify({'success':True})

@app.route('/api/stop', methods=['POST'])
def api_stop():
    global proc, last_error, conv_log_file, proc_started_at
    with proc_lock:
        last_error = ''
        if proc:
            try:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            try:
                # 确保进程已终止
                if proc.poll() is None:
                    import signal
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
            proc = None
            proc_started_at = None
        if conv_log_file:
            try: conv_log_file.close()
            except: pass
            conv_log_file = None
        cfg['enabled'] = False; save_cfg()
    return jsonify({'success':True})

@app.route('/api/logs')
def api_logs():
    # 统计(全量聚合)与明细列表(仅最近100条)分离：
    # 修复此前"统计只算最近100条"导致的不准确显示。
    stats = {'total_count': 0, 'success_count': 0, 'total_saved_mb': 0.0, 'total_processed_mb': 0.0}
    rows = []
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        try:
            agg = db.execute(
                'SELECT COUNT(*), '
                '       COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0), '
                '       COALESCE(SUM(saved_size), 0), '
                '       COALESCE(SUM(file_size), 0) '
                'FROM processed_files'
            ).fetchone()
            if agg:
                stats['total_count'] = int(agg[0] or 0)
                stats['success_count'] = int(agg[1] or 0)
                stats['total_saved_mb'] = round((agg[2] or 0) / 1048576, 2)
                stats['total_processed_mb'] = round((agg[3] or 0) / 1048576, 2)
            rows = db.execute('SELECT * FROM processed_files ORDER BY processed_at DESC LIMIT 100').fetchall()
        finally:
            db.close()
    except: pass
    logs = []
    for r in rows:
        logs.append({'id':r[0],'filepath':str(r[1]),'file_size':r[2],
            'file_size_mb':round(r[2]/1048576,2),'processed_at':str(r[3]),
            'success':bool(r[4]),'saved_size':r[5] or 0,
            'saved_size_mb':round((r[5] or 0)/1048576,2)})
    return jsonify({'logs':logs,
                    'total_saved_mb':stats['total_saved_mb'],
                    'total_count':stats['total_count'],
                    'success_count':stats['success_count'],
                    'total_processed_mb':stats['total_processed_mb']})

@app.route('/api/problems')
def api_problems():
    _ensure_problem_schema()
    items = []
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            rows = db.execute('SELECT filepath, reason, first_failed_at, last_attempt_at, failure_count, file_size FROM problem_files ORDER BY last_attempt_at DESC').fetchall()
            # 自清理：文件已不存在的记录直接删除，不再等转码进程重启
            stale = [r[0] for r in rows if not os.path.exists(r[0])]
            if stale:
                db.executemany('DELETE FROM problem_files WHERE filepath = ?', [(s,) for s in stale])
                db.commit()
                rows = [r for r in rows if os.path.exists(r[0])]
        finally:
            db.close()
        for r in rows:
            items.append({'filepath':str(r[0]),'reason':str(r[1] or ''),
                'first_failed_at':str(r[2]),'last_attempt_at':str(r[3]),
                'failure_count':r[4],'file_size_mb':round((r[5] or 0)/1048576,2),
                'exists':True})
    except Exception: pass
    return jsonify({'problems':items})

@app.route('/api/problems/delete', methods=['POST'])
def api_problem_delete():
    _ensure_problem_schema()
    data = request.get_json(silent=True) or {}
    fp = (data.get('filepath') or '').strip()
    if not fp:
        return jsonify({'success':False,'error':'缺少文件路径'}), 400
    # 仅允许删除问题文件表中登记的文件，防止误删任意路径
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            row = db.execute('SELECT filepath FROM problem_files WHERE filepath = ?', (fp,)).fetchone()
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success':False,'error':f'数据库错误: {e}'}), 500
    if not row:
        return jsonify({'success':False,'error':'该文件不在问题文件列表中，拒绝删除'}), 400
    if _is_blocked_system_path(fp):
        return jsonify({'success':False,'error':'禁止删除系统目录下的文件'}), 403
    if not _is_under_authorized_root(fp):
        return jsonify({'success':False,'error':'文件不在已授权目录内，拒绝删除'}), 403
    try:
        if os.path.isdir(fp):
            return jsonify({'success':False,'error':'仅支持删除文件，不支持目录'}), 400
        if os.path.exists(fp):
            os.remove(fp)
    except Exception as e:
        return jsonify({'success':False,'error':f'删除失败: {e}'}), 500
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            db.execute('DELETE FROM problem_files WHERE filepath = ?', (fp,))
            db.commit()
        finally:
            db.close()
    except Exception: pass
    return jsonify({'success':True})

@app.route('/api/problems/recheck', methods=['POST'])
def api_problem_recheck():
    _ensure_problem_schema()
    data = request.get_json(silent=True) or {}
    fp = (data.get('filepath') or '').strip()
    if not fp:
        return jsonify({'success':False,'error':'缺少文件路径'}), 400
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            db.execute('DELETE FROM problem_files WHERE filepath = ?', (fp,))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success':False,'error':f'数据库错误: {e}'}), 500
    return jsonify({'success':True})

@app.route('/api/problems/batch_delete', methods=['POST'])
def api_problem_batch_delete():
    """批量删除问题文件：与单删同样的安全白名单（必须在 problem_files 表中、
    不是系统路径、位于授权目录下），逐个删除文件并清理数据库记录。"""
    _ensure_problem_schema()
    data = request.get_json(silent=True) or {}
    fps = data.get('filepaths')
    if not isinstance(fps, list) or not fps:
        return jsonify({'success':False,'error':'未选择任何文件'}), 400
    fps = [str(x).strip() for x in fps if str(x).strip()]
    if not fps:
        return jsonify({'success':False,'error':'未选择任何文件'}), 400
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            whitelist = {r[0] for r in db.execute('SELECT filepath FROM problem_files').fetchall()}
        finally:
            db.close()
    except Exception as e:
        return jsonify({'success':False,'error':f'数据库错误: {e}'}), 500

    deleted, skipped, errors = [], [], []
    for fp in fps:
        if fp not in whitelist:
            skipped.append({'filepath': fp, 'reason': '不在问题列表中'}); continue
        if _is_blocked_system_path(fp):
            skipped.append({'filepath': fp, 'reason': '禁止删除系统目录下的文件'}); continue
        if not _is_under_authorized_root(fp):
            skipped.append({'filepath': fp, 'reason': '文件不在已授权目录内'}); continue
        try:
            if os.path.isdir(fp):
                skipped.append({'filepath': fp, 'reason': '仅支持删除文件，不支持目录'}); continue
            if os.path.exists(fp):
                os.remove(fp)
            deleted.append(fp)
        except Exception as e:
            errors.append({'filepath': fp, 'reason': str(e)})
    # 已成功删除物理文件的，从 problem_files 表中移除对应记录
    if deleted:
        try:
            db = sqlite3.connect(DB_PATH)
            try:
                db.executemany('DELETE FROM problem_files WHERE filepath = ?', [(fp,) for fp in deleted])
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
    return jsonify({'success': True, 'deleted_count': len(deleted),
                    'deleted': deleted, 'skipped': skipped, 'errors': errors})

@app.route('/api/browse')
def api_browse():
    p = request.args.get('path', '/')
    normalized, err = _normalize_abs_path(p)
    if not normalized:
        return jsonify({'error':err}), 400
    p = normalized
    entries = []
    def add_entry(name, path, is_dir=True, extra=None):
        if any(e.get('path') == path for e in entries):
            return
        item = {'name':name, 'path':path, 'is_dir':is_dir}
        if extra:
            item.update(extra)
        entries.append(item)
    roots = get_authorized_roots()
    if p == '/':
        # 根目录视图：仅展示用户在 fnOS"应用设置→授权目录"中授权的目录
        # 以及通过 config/resource:data-share 声明的共享目录。
        if not roots:
            return jsonify({
                'path': '/',
                'entries': [],
                'message': ('当前应用没有任何已授权的目录。\n'
                            '请前往 fnOS：应用 → 视频转码 → 应用设置 → 授权目录，\n'
                            '为需要监控的目录（例如 /vol3/1000/PORN）授予“读写”权限后再返回。')
            })
        for root in roots:
            try:
                if os.path.exists(root):
                    add_entry(root, root, os.path.isdir(root))
                else:
                    # 已授权但当前不存在（例如卷未挂载），仍展示让用户感知
                    add_entry(root + '（不可访问）', root, True, {'no_access': True})
            except OSError:
                add_entry(root + '（不可访问）', root, True, {'no_access': True})
        return jsonify({'path': '/', 'entries': entries})

    # 非根目录：必须位于已授权根之下
    if _is_blocked_system_path(p):
        return jsonify({'error': f'禁止浏览系统目录: {p}'}), 403
    if not _is_under_authorized_root(p):
        return jsonify({'error': (
            f'目录未授权访问: {p}\n'
            f'请前往“应用设置→授权目录”授予该目录的读写权限。'
        )}), 403
    if p.count('/') > 12:
        return jsonify({'error': 'Path too deep'}), 400
    try:
        items = sorted(os.listdir(p))
    except PermissionError:
        return jsonify({'error': '无权限访问此目录（请在应用设置中授予读写权限）'}), 403
    except FileNotFoundError:
        return jsonify({'error': '目录不存在'}), 404
    except OSError as e:
        return jsonify({'error': str(e)}), 500
    for item in items:
        if len(entries) >= 500:
            break
        full = os.path.join(p, item)
        try:
            is_dir = os.path.isdir(full)
            add_entry(item, full, is_dir)
        except PermissionError:
            add_entry(item, full, True, {'no_access': True})
        except OSError:
            pass
    return jsonify({'path': p, 'entries': entries})

HTML = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>视频转码</title><style>
:root{--bg:#0f1115;--bg2:#161922;--card:#1c2030;--card2:#232838;--border:#2a3045;--txt:#e5e7eb;--mut:#8b93a7;--acc:#38bdf8;--acc2:#8b5cf6;--ok:#10b981;--warn:#f59e0b;--err:#ef4444}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:var(--bg);color:var(--txt);min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(1200px 600px at 20% -10%,rgba(139,92,246,.15),transparent 60%),radial-gradient(900px 500px at 100% 0%,rgba(56,189,248,.1),transparent 60%);z-index:-1}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px}
.hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;padding:18px 22px;background:linear-gradient(135deg,rgba(139,92,246,.18),rgba(56,189,248,.1));border:1px solid var(--border);border-radius:14px;backdrop-filter:blur(10px)}
.hd h1{font-size:20px;font-weight:600;letter-spacing:.5px}
.hd p{color:var(--mut);font-size:12px;margin-top:4px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-size:12px;font-weight:500}
.badge::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}
.b-on{background:rgba(16,185,129,.15);color:#34d399}
.b-off{background:rgba(239,68,68,.15);color:#f87171}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:6px;margin-bottom:18px;overflow-x:auto}
.tab{padding:9px 18px;border-radius:8px;font-size:13px;color:var(--mut);cursor:pointer;border:none;background:transparent;white-space:nowrap;transition:.15s;display:flex;align-items:center;gap:8px}
.tab:hover{color:var(--txt);background:rgba(255,255,255,.04)}
.tab.active{background:linear-gradient(135deg,var(--acc2),var(--acc));color:#fff;font-weight:500}
.tab .cnt{background:rgba(239,68,68,.9);color:#fff;font-size:11px;padding:1px 7px;border-radius:999px;min-width:20px;text-align:center}
.pane{display:none}
.pane.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:14px}
.card h3{font-size:13px;font-weight:600;color:var(--mut);text-transform:uppercase;letter-spacing:1px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between}
.row{display:flex;gap:10px;flex-wrap:wrap}
.btn{padding:9px 18px;border:none;border-radius:8px;font-size:13px;font-weight:500;cursor:pointer;transition:.15s;display:inline-flex;align-items:center;gap:6px}
.btn:active{transform:scale(.97)}
.b-acc{background:linear-gradient(135deg,var(--acc2),var(--acc));color:#fff}
.b-ok{background:rgba(16,185,129,.9);color:#fff}
.b-err{background:rgba(239,68,68,.9);color:#fff}
.b-ghost{background:var(--card2);color:var(--txt);border:1px solid var(--border)}
.snap{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:14px}
.kv{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.kv .k{font-size:11px;color:var(--mut);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.kv .v{font-size:13px;word-break:break-all}
.pbar{height:8px;background:var(--card2);border-radius:99px;overflow:hidden;margin-top:8px}
.pfill{height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .3s}
.ptext{font-size:11px;color:var(--mut);margin-top:5px;text-align:right}
.logbox{background:#0a0c12;color:#a5b4cf;border-radius:10px;padding:12px;max-height:240px;overflow:auto;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;white-space:pre-wrap;border:1px solid var(--border)}
.errbox{background:rgba(239,68,68,.08);color:#fca5a5;border:1px solid rgba(239,68,68,.3);border-radius:10px;padding:10px 12px;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:120px;overflow:auto}
.errmsg{color:#fca5a5;font-size:12px;margin-top:8px;padding:8px 12px;background:rgba(239,68,68,.1);border-radius:8px;display:none}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--card2);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}
.stat .v{font-size:26px;font-weight:700;background:linear-gradient(135deg,var(--acc),var(--acc2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat .l{color:var(--mut);font-size:12px;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--mut);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
tr:hover td{background:rgba(255,255,255,.02)}
.suc{color:#34d399}.err{color:#f87171}
.tc{overflow-x:auto}
.fg{margin-bottom:12px}
.fg label{display:block;margin-bottom:5px;font-size:12px;color:var(--mut);font-weight:500}
input,select{width:100%;padding:9px 12px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--txt);font-size:13px;font-family:inherit}
input:focus,select:focus{outline:none;border-color:var(--acc)}
input[type=checkbox]{width:auto}
.dir_item{display:flex;justify-content:space-between;align-items:center;gap:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:7px 10px;margin-bottom:6px;font-size:12px}
.dir_item span{flex:1;word-break:break-all}
.info{color:var(--mut);font-size:11px;margin-top:6px}
.muted{color:var(--mut);font-size:11px}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:999;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.modal-box{background:var(--card);border:1px solid var(--border);border-radius:12px;width:90%;max-width:560px;max-height:70vh;display:flex;flex-direction:column}
.bitem{padding:10px 12px;cursor:pointer;border-radius:8px;font-size:13px;color:var(--txt)}
.bitem:hover{background:rgba(56,189,248,.1)}
</style></head><body><div class="wrap">
<div class="hd">
<div><h1>视频转码</h1><p>自动监测文件夹 · ffmpeg 硬件加速 · 节省空间</p></div>
<span id="badge" class="badge b-off">已停止</span>
</div>
<div id="errmsg" class="errmsg"></div>

<div class="tabs">
<button class="tab active" data-tab="dash" onclick="showTab('dash')">控制面板</button>
<button class="tab" data-tab="prob" onclick="showTab('prob')">问题文件 <span class="cnt" id="prob_cnt" style="display:none">0</span></button>
<button class="tab" data-tab="log" onclick="showTab('log')">转码日志</button>
<button class="tab" data-tab="cfg" onclick="showTab('cfg')">设置</button>
</div>

<div class="pane active" id="pane-dash">
<div class="card">
<h3>服务控制</h3>
<div class="row">
<button class="btn b-ok" onclick="api('start')">▶ 启动</button>
<button class="btn b-err" onclick="api('stop')">■ 停止</button>
</div>
</div>
<div class="snap">
<div class="kv"><div class="k">PID</div><div class="v" id="process_pid">-</div></div>
<div class="kv"><div class="k">运行时长</div><div class="v" id="process_uptime">-</div></div>
<div class="kv"><div class="k">当前文件</div><div class="v" id="current_file">-</div></div>
<div class="kv"><div class="k">当前状态</div><div class="v" id="current_activity">-</div></div>
</div>
<div class="card">
<h3>转码进度</h3>
<div class="pbar"><div class="pfill" id="progress_fill"></div></div>
<div class="ptext" id="progress_text">0%</div>
</div>
<div class="card">
<h3>最近错误</h3>
<div class="errbox" id="last_error_text">无</div>
</div>
<div class="card">
<h3>最近输出日志</h3>
<pre class="logbox" id="recent_log">暂无日志</pre>
</div>
</div>

<div class="pane" id="pane-prob">
<div class="card">
<h3>问题文件（损坏 / 转码失败）
<div class="row">
<button class="btn b-ghost" style="padding:5px 12px;font-size:11px" onclick="loadProblems()">刷新</button>
<button class="btn b-err" style="padding:5px 12px;font-size:11px" onclick="batchDeleteProblems()">批量删除选中</button>
</div></h3>
<div id="prob_empty" class="info" style="padding:20px 0;text-align:center">暂无问题文件</div>
<div class="tc" id="prob_wrap" style="display:none">
<table><thead><tr>
<th style="width:32px"><input type="checkbox" id="prob_select_all" onclick="toggleAllProblems(this)" title="全选/反选"></th>
<th>文件</th><th>失败原因</th><th>重试</th><th>大小(MB)</th><th>操作</th>
</tr></thead><tbody id="prob_tb"></tbody></table>
</div>
<div class="info" style="margin-top:10px">达重试上限的文件会进入此列表并被静默跳过。勾选后可批量永久删除（不可恢复），或单条"重新检测"。</div>
</div>
</div>

<div class="pane" id="pane-log">
<div class="stats">
<div class="stat"><div class="v" id="ts">0</div><div class="l">节省空间 (MB)</div></div>
<div class="stat"><div class="v" id="tc2">0</div><div class="l">处理文件总数</div></div>
</div>
<div class="card">
<h3>转码记录</h3>
<div class="tc"><table><thead><tr><th>文件</th><th>原大小(MB)</th><th>节省(MB)</th><th>状态</th><th>时间</th></tr></thead><tbody id="tb"></tbody></table></div>
</div>
</div>

<div class="pane" id="pane-cfg">
<div class="card">
<h3>监控目录</h3>
<div id="dirs_list"></div>
<div style="display:flex;gap:8px;margin-bottom:10px">
<input type="text" id="monitor_dir" value="{{config.monitor_dir}}" placeholder="如 /vol3/1000/视频">
<button class="btn b-ghost" onclick="openBrowser()" style="white-space:nowrap">浏览</button>
<button class="btn b-acc" onclick="addDir()" style="white-space:nowrap">添加</button>
</div>
<div class="info">每轮扫描一次，可同时监控多个目录。</div>
</div>
<div class="card">
<h3>转码参数</h3>
<div class="fg"><label>分辨率策略</label><select id="resize_mode" onchange="toggleResizeCustom()">
<option value="1080p" {%if config.resize_mode=="1080p"%}selected{%endif%}>缩放至 1080p</option>
<option value="keep" {%if config.resize_mode=="keep"%}selected{%endif%}>保持原分辨率</option>
<option value="custom" {%if config.resize_mode=="custom"%}selected{%endif%}>自定义</option>
</select></div>
<div class="fg" id="custom_resize_wrap" style="display:none"><label>自定义分辨率（如 -2:1080 / 1920x-2 / scale=-2:1080）</label><input type="text" id="custom_resize" value="{{config.custom_resize}}" placeholder="-2:1080"></div>
<div class="fg"><label>CRF（18-32，越小质量越高）</label><input type="number" id="crf" min="18" max="32" value="{{config.crf}}"></div>
<div class="fg"><label>编码预设（越慢越省空间）</label><select id="preset">{% for p in ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow'] %}<option value="{{p}}" {%if p==config.preset%}selected{%endif%}>{{p}}</option>{%endfor%}</select></div>
<div class="fg"><label>线程数（最多 4）</label><input type="number" id="threads" min="1" max="4" value="{{config.threads}}"></div>
<div class="fg"><label>编码器</label><select id="codec">
<option value="libx264" {%if config.codec=="libx264"%}selected{%endif%}>H.264（兼容性好）</option>
<option value="libx265" {%if config.codec=="libx265"%}selected{%endif%}>H.265（更省空间）</option>
</select></div>
<div class="fg"><label>封装格式</label><select id="container">
<option value="mp4" {%if config.container=="mp4"%}selected{%endif%}>MP4</option>
<option value="mkv" {%if config.container=="mkv"%}selected{%endif%}>MKV</option>
</select></div>
<div class="fg"><label style="display:flex;align-items:center;gap:8px;cursor:pointer"><input type="checkbox" id="use_gpu" {%if config.use_gpu%}checked{%endif%}> GPU 加速（Intel QSV）</label></div>
<button class="btn b-acc" onclick="saveCfg()">保存配置</button>
<div class="muted" style="margin-top:10px">日志: {{conv_log}} · 数据库: {{db_path}}</div>
</div>
</div>
</div>

<div id="modal" class="modal"><div class="modal-box">
<div style="padding:14px 18px;border-bottom:1px solid var(--border)">
<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px">
<span id="bpath" style="font-weight:600;font-size:13px;word-break:break-all">/</span>
<div style="display:flex;gap:6px">
<button class="btn b-ok" style="padding:5px 12px;font-size:12px" onclick="selectDir()">选择此目录</button>
<button class="btn b-ghost" style="padding:5px 12px;font-size:12px" onclick="el('modal').style.display='none'">关闭</button>
</div></div>
<div style="display:flex;gap:8px">
<input id="browser_path_input" type="text" placeholder="可直接输入授权目录">
<button class="btn b-acc" style="padding:8px 12px;white-space:nowrap" onclick="openBrowserFromInput()">打开</button>
</div></div>
<div id="blist" style="overflow-y:auto;flex:1;padding:8px 12px"></div>
</div></div>

<script>
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===name));document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('active',p.id==='pane-'+name))}
function el(id){return document.getElementById(id)}
function api(a){fetch('/api/'+a,{method:'POST'}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(d=>{if(d.error){el('errmsg').textContent=d.error;el('errmsg').style.display='block'}else{el('errmsg').style.display='none'}refresh()}).catch(e=>{el('errmsg').textContent='请求失败: '+e.message;el('errmsg').style.display='block'})}
var monitorDirs=({{config.monitor_dirs|tojson}}||[]);
function addDir(){let v=(el('monitor_dir').value||'').trim();if(!v){alert('请输入目录路径');return}v=v.replace(/\\/+$/,'');if(monitorDirs.indexOf(v)<0){monitorDirs.push(v);renderDirs();saveCfg()}else{alert('该目录已在列表中')}}
function removeDir(i){monitorDirs.splice(i,1);renderDirs();saveCfg()}
function renderDirs(){let L=el('dirs_list');L.innerHTML='';if(!monitorDirs.length){L.innerHTML='<div class="info" style="margin-bottom:6px">尚未添加任何监控目录</div>';return}monitorDirs.forEach((d,i)=>{let w=document.createElement('div');w.className='dir_item';let sp=document.createElement('span');sp.textContent=d;let rm=document.createElement('button');rm.textContent='删除';rm.className='btn b-err';rm.style.cssText='padding:3px 10px;font-size:11px;flex-shrink:0';rm.onclick=()=>removeDir(i);w.appendChild(sp);w.appendChild(rm);L.appendChild(w)})}
function toggleResizeCustom(){el('custom_resize_wrap').style.display=(el('resize_mode').value==='custom')?'block':'none'}
function saveCfg(){let d={monitor_dirs:monitorDirs,crf:parseInt(el('crf').value),preset:el('preset').value,threads:parseInt(el('threads').value),codec:el('codec').value,container:el('container').value,use_gpu:el('use_gpu').checked,resize_mode:el('resize_mode').value,custom_resize:el('custom_resize').value};fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(d=>{if(d.success)alert('已保存');else alert('保存失败')}).catch(e=>alert('保存失败: '+e.message))}
async function refresh(){try{let s=await fetch('/api/status').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});let b=el('badge');b.textContent=s.running?'运行中':'已停止';b.className='badge '+(s.running?'b-on':'b-off');if(s.config){el('monitor_dir').value=s.config.monitor_dir;el('crf').value=s.config.crf;el('preset').value=s.config.preset;el('threads').value=s.config.threads;el('codec').value=s.config.codec;el('container').value=s.config.container;el('use_gpu').checked=s.config.use_gpu!==false;el('resize_mode').value=s.config.resize_mode||'1080p';el('custom_resize').value=s.config.custom_resize||'';if(s.config.monitor_dirs){let nd=(s.config.monitor_dirs||[]).filter(x=>x);if(nd.join('|')!==monitorDirs.join('|')){monitorDirs=nd;renderDirs()}}}toggleResizeCustom();let p=s.process||{};el('process_pid').textContent=p.pid||'-';el('process_uptime').textContent=p.uptime_seconds!=null?(p.uptime_seconds+' 秒'):'-';el('current_file').textContent=p.current_file||'-';el('current_activity').textContent=p.current_activity||'-';let pct=Math.max(0,Math.min(100,Number(p.progress_percent||0)));el('progress_fill').style.width=pct+'%';el('progress_text').textContent=pct.toFixed(1)+'%';el('last_error_text').textContent=s.error||p.last_error||'无';el('recent_log').textContent=(s.recent_log&&s.recent_log.length)?s.recent_log.join('\\n'):'暂无日志';let l=await fetch('/api/logs').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});el('ts').textContent=l.total_saved_mb;el('tc2').textContent=l.total_count!=null?l.total_count:l.logs.length;let t=el('tb');t.innerHTML='';l.logs.forEach(r=>{let tr=document.createElement('tr');['filepath','file_size_mb','saved_size_mb'].forEach(k=>{let td=document.createElement('td');td.textContent=r[k];tr.appendChild(td)});let sd=document.createElement('td');sd.textContent=r.success?'成功':'失败';sd.className=r.success?'suc':'err';tr.appendChild(sd);let td=document.createElement('td');td.textContent=r.processed_at;tr.appendChild(td);t.appendChild(tr)})}catch(e){console.error('refresh error:',e)}}
var browsePath='/';
async function openBrowser(p){if(p){browsePath=p}else{let v=(el('monitor_dir').value||'').trim();browsePath=v.startsWith('/')?v:'/'}try{let d=await fetch('/api/browse?path='+encodeURIComponent(browsePath)).then(r=>{if(!r.ok)return r.json().then(j=>{throw new Error(j.error||r.status)});return r.json()});if(d.error){alert(d.error);return}let m=el('modal'),lst=el('blist');browsePath=d.path;el('bpath').textContent=d.path;if(el('browser_path_input'))el('browser_path_input').value=d.path;lst.innerHTML='';if(d.message){let tip=document.createElement('div');tip.style.cssText='padding:10px 12px;background:rgba(245,158,11,.15);color:#fbbf24;border-radius:8px;margin-bottom:8px;white-space:pre-wrap;font-size:12px;line-height:1.5';tip.textContent=d.message;lst.appendChild(tip)}if(d.path!=='/'){let b=document.createElement('div');b.className='bitem';b.textContent='.. 返回上级';b.onclick=()=>openBrowser(d.path.split('/').slice(0,-1).join('/')||'/');lst.appendChild(b)}d.entries.forEach(e=>{let b=document.createElement('div');b.className='bitem';b.textContent=e.name+(e.is_dir?'/':'');if(e.no_access){b.style.opacity='0.4';if(e.is_dir)b.onclick=()=>alert('无权限访问此目录')}else if(e.is_dir){b.onclick=()=>openBrowser(e.path)}else{b.style.opacity='0.5'}lst.appendChild(b)});m.style.display='flex'}catch(e){alert('浏览目录失败: '+e.message)}}
function openBrowserFromInput(){let p=(el('browser_path_input').value||'').trim();if(!p){alert('请输入完整路径');return}openBrowser(p)}
function selectDir(){let v=(browsePath||'').replace(/\\/+$/,'');el('monitor_dir').value=v;el('modal').style.display='none';if(v&&monitorDirs.indexOf(v)<0){monitorDirs.push(v);renderDirs()}saveCfg()}
async function loadProblems(){try{let d=await fetch('/api/problems').then(r=>{if(!r.ok)throw new Error(r.status);return r.json()});let lst=(d.problems||[]).filter(p=>p.exists);let ne=el('prob_empty'),wr=el('prob_wrap'),tb=el('prob_tb'),cnt=el('prob_cnt');if(!ne||!wr||!tb)return;let sa=el('prob_select_all');if(sa)sa.checked=false;if(cnt){if(lst.length>0){cnt.style.display='';cnt.textContent=lst.length}else{cnt.style.display='none'}}if(!lst.length){ne.style.display='';wr.style.display='none';return}ne.style.display='none';wr.style.display='';tb.innerHTML='';lst.forEach(p=>{let tr=document.createElement('tr');let td0=document.createElement('td');let cb=document.createElement('input');cb.type='checkbox';cb.className='prob_cb';cb.value=p.filepath;td0.appendChild(cb);tr.appendChild(td0);let td1=document.createElement('td');td1.textContent=p.filepath;td1.style.wordBreak='break-all';tr.appendChild(td1);let td2=document.createElement('td');td2.textContent=p.reason||'转码失败';td2.className='err';tr.appendChild(td2);let td3=document.createElement('td');td3.textContent=p.failure_count;tr.appendChild(td3);let td4=document.createElement('td');td4.textContent=p.file_size_mb;tr.appendChild(td4);let td5=document.createElement('td');td5.style.whiteSpace='nowrap';let rb=document.createElement('button');rb.textContent='重测';rb.className='btn b-ghost';rb.style.cssText='padding:3px 10px;font-size:11px;margin-right:6px';rb.onclick=()=>probRecheck(p.filepath);td5.appendChild(rb);let db2=document.createElement('button');db2.textContent='删除';db2.className='btn b-err';db2.style.cssText='padding:3px 10px;font-size:11px';db2.onclick=()=>probDelete(p.filepath);td5.appendChild(db2);tr.appendChild(td5);tb.appendChild(tr)})}catch(e){console.error('loadProblems error:',e)}}
function toggleAllProblems(master){document.querySelectorAll('.prob_cb').forEach(c=>{c.checked=master.checked})}
async function batchDeleteProblems(){let fps=[];document.querySelectorAll('.prob_cb').forEach(c=>{if(c.checked)fps.push(c.value)});if(!fps.length){alert('请先勾选要删除的问题文件');return}if(!confirm('确定要永久删除选中的 '+fps.length+' 个文件吗？\\n\\n此操作不可恢复！'))return;try{let r=await fetch('/api/problems/batch_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filepaths:fps})});let d=await r.json();if(d.success){let msg='已删除 '+d.deleted_count+' 个文件';if(d.skipped&&d.skipped.length)msg+='\\n跳过 '+d.skipped.length+' 个';if(d.errors&&d.errors.length)msg+='\\n失败 '+d.errors.length+' 个';alert(msg);loadProblems()}else{alert(d.error||'批量删除失败')}}catch(e){alert('批量删除失败: '+e.message)}}
function probRecheck(fp){fetch('/api/problems/recheck',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filepath:fp})}).then(r=>r.json()).then(d=>{if(d.success){alert('已从问题列表移除');loadProblems()}else{alert(d.error||'操作失败')}}).catch(e=>alert('操作失败: '+e.message))}
function probDelete(fp){if(!confirm('确定要永久删除该文件吗？\\n\\n'+fp+'\\n\\n此操作不可恢复！'))return;fetch('/api/problems/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filepath:fp})}).then(r=>r.json()).then(d=>{if(d.success){alert('文件已删除');loadProblems()}else{alert(d.error||'删除失败')}}).catch(e=>alert('删除失败: '+e.message))}
renderDirs();toggleResizeCustom();refresh();setInterval(refresh,5000);loadProblems()</script>
</body></html>'''

if __name__ == '__main__':
    import signal
    def _sigterm(sig, frame):
        global proc, conv_log_file
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            proc = None
        if conv_log_file:
            try: conv_log_file.close()
            except: pass
            conv_log_file = None
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sigterm)
    print(f'Starting on http://0.0.0.0:5000 (packages:{PKG_DIR})', flush=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
