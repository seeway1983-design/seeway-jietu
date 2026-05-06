# Windows 迁移说明

这个目录用于把 `pupu_price_mvp` 搬到：

- Parallels Desktop 里的 Windows
- 独立 Windows 电脑

优先目标：

- `Windows + MuMu/雷电`
- `朴朴 App`
- `1 个城市 + 卫龙 + 搜索结果页截图`

## 推荐迁移顺序

1. 把整个 `pupu_price_mvp_windows_bundle` 目录复制到 Windows
2. 安装并启动 MuMu 或雷电
3. 在模拟器里安装并登录朴朴 App
4. 安装 Python 3.10+
5. 安装 Node.js LTS
6. 用 PowerShell 运行 `setup_windows_mvp.ps1`
7. 用 `python run_mvp.py --doctor` 检查环境

## Windows 侧最低准备

- `adb` 可用
- `node` / `npm` 可用
- `appium` 可用
- Android 模拟器已启动
- `adb devices` 能看到设备

## 运行命令

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows\setup_windows_mvp.ps1
```

环境检查：

```powershell
python .\run_mvp.py --doctor
```

查看执行计划：

```powershell
python .\run_mvp.py --city 佛山市 --brand 卫龙 --plan
```

## 需要你手动完成的部分

- 登录 MuMu/雷电账号
- 登录朴朴 App
- 确认模拟器里已进入朴朴首页
- 首次用 Appium Inspector / UIAutomatorViewer 补真实控件定位

## 当前结论

这套包的目标不是在 Mac 上继续跑，而是把当前安卓路线平移到 Windows，再从那里验证真正可执行的 MVP。
