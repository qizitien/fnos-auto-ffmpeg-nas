# fpkconverter（飞牛 fnOS 视频转码）

飞牛 NAS 上的本地视频自动转码/压缩工具。基于 Flask + ffmpeg，支持 Intel QSV 硬件加速，深色影音风界面。

## 功能

- 多目录监控，定时扫描新视频
- ffmpeg 转码：libx264 / libx265 / Intel QSV 硬件加速（失败自动 CPU 回退）
- 分辨率策略：1080p / 保持原分辨率 / 自定义 scale
- CRF 18-32、线程数 1-4、码率控制
- 失败文件自动重试（30 分钟 / 2 小时 / 6 小时，共 3 次）
- 问题文件列表 + 全选 + 批量删除选中
- 实时进度、最近错误、最近输出日志
- 节省空间统计

## 目录结构

```
├── manifest                 # 飞牛应用元信息（appname/version/display_name）
├── cmd/                     # fnOS 生命周期脚本（main/config_callback/...）
├── config/                  # privilege / resource（运行权限声明）
├── wizard/                  # 安装/卸载向导文案
├── ui/config                # 桌面 iframe 配置
├── ICON.PNG / ICON_256.PNG  # 应用图标
└── app/fpkconverter/
    ├── web_server.py        # Flask Web 服务（UI + 所有 REST API）
    ├── fpk_converter.py     # 转码核心（数据库、ffmpeg 调用、watchdog 监控）
    └── requirements.txt      # Python 依赖（flask / watchdog / werkzeug）
```

## 本地运行（开发调试）

```bash
# 安装依赖到 packages/ 目录（应用启动时会自动加入 PYTHONPATH）
pip install -r app/fpkconverter/requirements.txt -t app/fpkconverter/packages

# 启动
cd app/fpkconverter
python3 web_server.py
# 访问 http://localhost:5000
```

## 打包成 .fpk

飞牛 fpk 本质是两层 tar.gz：

1. 先把 `app/` 目录打成 `app.tgz`（gzip tar）
2. 再把 `app.tgz` + `cmd/` + `config/` + `wizard/` + `manifest` + `ICON*.PNG` 打成最终 .fpk（gzip tar）

```bash
# 在项目根目录
tar czf app.tgz -C app .
tar czf fpkconverter.fpk app.tgz cmd config wizard manifest ICON.PNG ICON_256.PNG
```

## 版本

- v1.0.58：深色影音风界面 + Tab 分栏（控制面板 / 问题文件 / 转码日志 / 设置）
- v1.0.57：问题文件全选 + 批量删除选中
- v1.0.56：初始版本

## 作者

yang
