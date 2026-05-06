# Mac mini 部署说明

本文档用于把 `pupu_price_mvp` 部署到公司 Mac mini，并以 Android 真机作为生产执行设备。

## 1. 首次部署

```bash
git clone <你的 GitHub 仓库地址>
cd pupu_price_mvp
bash setup_macmini.sh
```

## 2. 创建 Python 虚拟环境

如果你希望手动执行，也可以按下面步骤：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 3. 必需系统工具

本项目当前主流程实际依赖以下系统工具：

- `adb`
- `node`
- `appium`
- `curl`

推荐安装方式：

```bash
brew install android-platform-tools node
npm install -g appium
```

说明：

- 当前代码中没有实际使用 `playwright`。
- 当前代码中没有实际使用 `ffmpeg`。
- 如后续流程扩展到其他调试链路，再按需补装。

## 4. 本地配置

首次部署后，创建本机私有配置文件：

```bash
cp config/device.json config/device.local.json
cp config/real_device_profile.json config/real_device_profile.local.json
```

然后按 Mac mini 实际环境修改：

- `config/device.local.json`
  - 填写 `adb_serial`
  - 确认 `appium_server_url`
- `config/real_device_profile.local.json`
  - 如更换同型号同分辨率设备，通常可先沿用
  - 如点击坐标不准，再按新设备调整

注意：

- `*.local.json` 已被 `.gitignore` 排除，不会提交到 GitHub。
- 仓库内 `config/device.json` 是模板，不应直接写入真实设备序列号。

## 5. 如何运行

先连接 Android 真机，并确认：

```bash
adb devices
```

启动 Appium：

```bash
bash tools/start_appium_server.sh
```

做环境检查：

```bash
python3 run_mvp.py --doctor
```

日常运行：

```bash
bash run.sh
```

## 6. 如何更新

```bash
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

## 7. 常见报错排查

### `adb: command not found`

- 执行 `brew install android-platform-tools`
- 确认 `adb devices` 可用

### `appium: command not found`

- 执行 `npm install -g appium`
- 确认 `appium --version` 可用

### `当前没有可用 adb 设备`

- 检查 USB 线
- 检查手机开发者选项与 USB 调试
- 重新执行 `adb devices`

### `appium server: missing/unreachable`

- 执行 `bash tools/start_appium_server.sh`
- 再执行 `python3 run_mvp.py --doctor`

### 点击位置不准或流程跑偏

- 检查 `config/real_device_profile.local.json`
- 如果换了不同分辨率或不同系统版本的手机，需要重新校准坐标

### 朴朴 App 无法进入结果页

- 先人工确认手机网络正常
- 确认朴朴 App 已登录并可正常搜索
