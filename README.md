# 朴朴价格巡检项目

生产 SOP 与项目文档入口见 [docs/index.md](docs/index.md)。

## 项目目标

本项目用于按固定城市和品牌，在朴朴 App 中采集品牌搜索结果页 H5 长图，并通过离线清洗、质检和门禁，形成可发布的交付候选图。

当前生产目标：

- 按 `config/cities.json` 中 `enabled=true` 的城市执行。
- 按 `config/brands.json` 中 `enabled=true` 的品牌执行。
- 输出城市 × 品牌维度的 H5 图。
- 保留原始 H5，同时生成 cleaned H5 作为交付候选。
- 交付包生成是发布动作，不是每日运行必选动作。

## 当前主路线

当前主方案是：

```text
Mac 主控
  -> Android 真机
  -> 朴朴 App
  -> adb/Appium/UIAutomator2
  -> 高重叠真实分页截图
  -> H5 交付图
  -> 离线分析与 cleaned 重建
  -> cleaned 质检
  -> gatekeeper 总门禁
```

核心原则：

- 高重叠真实截图 + H5 交付图是当前主方案。
- 原始 H5 保留在 `deliverables_h5/`，不直接删除或覆盖历史图。
- cleaned H5 输出到 `deliverables_h5_cleaned/`，是通过清洗和质检后的交付候选。
- reports 是审计、排查和门禁依据。
- delivery package 是发布动作，只有 gatekeeper 通过后才允许生成。
- 地址门禁是 cleaned、gatekeeper 和交付包之前的硬门槛：所有城市必须有非空 `selected_address_text`、`delivery_available=true`、`city_switch_verified=true`。

## 当前暂停路线

以下路线已经验证过或踩过坑，当前暂停：

- 电脑微信小程序路线暂停。
- Mac 官方 Android Emulator 路线暂停。
- native-longshot 已验证，但当前设备自动化不可用，暂停。
- 第三方截屏工具暂停。
- 无缝拼接长图不是当前主方案，容易产生断层、重复和残片。
- 商品卡片重排不是当前主交付方案；当前 cleaned H5 是交付候选，不替代发布包流程。
- 不做接口抓取，不使用大模型识别商品，不新增截图模式。

## 当前目录结构

```text
pupu_price_mvp/
  config/
    cities.json
    brands.json
    device.json
    appium_capabilities.json
  debug/
    h5_pages/
  deliverables_h5/
  deliverables_h5_cleaned/
  delivery_packages/
  docs/
  reports/
  tools/
    h5_delivery_postprocess.py
    start_appium_server.sh
  run_mvp.py
  README.md
  handoff.md
```

## 产物目录说明

`deliverables_h5/`

- 原始 H5 采集产物。
- 保留历史，不作为清洗后的最终候选。
- 可能包含拼接断层、重复 SKU、推荐区混入或详情页误入等问题。

`debug/h5_pages/`

- 高重叠分页截图和分析底座。
- cleaned 重建主要依赖这些分页截图，而不是盲信原始 H5 长图。

`reports/YYYY-MM-DD/h5_cleaning/`

- 离线分析、卡片预览、summary、manifest、quality check、gatekeeper 报告。
- 是问题定位和交付放行的审计依据。

`deliverables_h5_cleaned/`

- cleaned H5 输出目录。
- 只包含 `delivery_grade=fixable` 且 rebuilt 图存在的样本。
- `delivery_grade=pass` 表示原始 H5 已可用，无需进入 cleaned 重建。
- 是交付候选，不等于已经发布。

`delivery_packages/`

- 发布包目录。
- 只有 gatekeeper 通过且用户确认发布时才生成。

## 每日推荐执行流程

1. 确认配置：检查 `config/cities.json` 和 `config/brands.json` 的 `enabled=true`。
2. 启动 Appium：`bash tools/start_appium_server.sh`。
3. 环境校验：`python3 run_mvp.py --doctor`。
4. 采集 H5：按目标日期、城市、品牌执行采集。
5. 地址门禁：所有城市 `selected_address_text` 非空、`delivery_available=true`、`city_switch_verified=true`。
6. 失败补跑：只补跑失败样本，不盲目全量重采。
7. 全量离线分析：刷新 `_batch_summary.csv/json`。
8. bug 闭环：一处 bug 一处验证，先单样本，后回归样本。
9. cleaned 重建：只处理 `delivery_grade=fixable`；`delivery_grade=pass` 视为无需清洗。
10. cleaned 质检：检查文件、路径、数量、尺寸、状态。
11. rerun_required 检查：必须为空。
12. gatekeeper 总门禁：所有 gate 通过才允许进入发布动作。
13. 交付包生成：用户明确确认后再执行。

## 常用命令

打印配置：

```bash
python3 run_mvp.py --print-config
```

环境校验：

```bash
python3 run_mvp.py --doctor
```

打印 Appium 启动命令：

```bash
python3 run_mvp.py --print-start-appium
```

启动 Appium：

```bash
bash tools/start_appium_server.sh
```

单样本 H5 采集：

```bash
python3 run_mvp.py --capture-one --city 武汉市 --brand 卫龙 --date 2026-05-04 --strict --fast --overwrite --output-mode h5 --debug-recommendation
```

全量离线分析：

```bash
python3 tools/h5_delivery_postprocess.py --date 2026-05-04 --all
```

单样本离线分析：

```bash
python3 tools/h5_delivery_postprocess.py --date 2026-05-04 --brand 卫龙 --city 泉州市
```

cleaned 重建：

```bash
python3 tools/h5_delivery_postprocess.py --date 2026-05-04 --generate-cleaned
```

本地品牌 ZIP 打包：

```bash
python3 tools/package_daily_delivery.py --date 2026-05-04
```

默认不覆盖已有 ZIP；如需重新生成同日品牌 ZIP：

```bash
python3 tools/package_daily_delivery.py --date 2026-05-04 --overwrite-package
```

本地 ZIP 打包只允许在 gatekeeper 通过后执行。交付源只允许来自 `deliverables_h5_cleaned/`，不使用原始 `deliverables_h5/`、失败截图、debug 图或中间预览图。

输出目录：

```text
delivery_packages/YYYY-MM-DD/
  by_brand/
    品牌巡价YYYY年M月D日.zip
  manifest/
    delivery_manifest.csv
    delivery_manifest.md
    package_summary.json
```

ZIP 命名规则：

- 品牌 ZIP：`品牌巡价YYYY年M月D日.zip`，例如 `卫龙巡价2026年5月4日.zip`。
- ZIP 内图片：`品牌-城市-YYYY.MM.DD.png`，城市名去掉“市”，例如 `卫龙-福州-2026.05.04.png`。

当前尚未接飞书上传，下一步才是飞书云盘上传。

## gatekeeper 门禁说明

gatekeeper 是交付包前的总门禁。数量必须动态计算：

```text
enabled_city_count = config/cities.json 中 enabled=true 的城市数量
enabled_brand_count = config/brands.json 中 enabled=true 的品牌数量
expected_total = enabled_city_count × enabled_brand_count
```

不能写死城市数、品牌数或总数。

gatekeeper 之前必须先过地址门禁：

- 所有 enabled 城市都必须有非空 `selected_address_text`。
- 所有 enabled 城市都必须 `delivery_available=true`。
- 所有 enabled 城市都必须 `city_switch_verified=true`。
- 任一城市不满足时，禁止进入 cleaned、gatekeeper 和交付包。

gatekeeper 必须确认：

- batch summary 行数等于 `expected_total`。
- cleaned manifest 行数等于 `expected_total`。
- 每个 enabled 品牌都有 `enabled_city_count` 张。
- 每个 enabled 城市都有 `enabled_brand_count` 张。
- 没有 `delivery_grade=fail`。
- `possible_missing_sku=0`。
- cleaned 文件全部存在，且都在 `deliverables_h5_cleaned/`。
- quality check 全部 pass，warning 和 fail 均为 0。
- `rerun_required` 为空。
- 业务统计字段完整保留。

## 失败时应该看哪里

- 采集失败：看 `reports/YYYY-MM-DD/*_capture_summary.csv/json` 和 `screenshots_failed/`。
- 人工复核 SKU 差异：先看 capture summary 里的 `target_city`、`address_keyword`、`address_candidates`、`attempted_address_candidates`、`selected_address_keyword`、`selected_address_text`、`delivery_available`、`address_match_warning`，确认人工手机使用的是同城同收货地址。
- H5 分页问题：看 `debug/h5_pages/YYYY-MM-DD/品牌/城市/`。
- 离线分析问题：看 `reports/YYYY-MM-DD/h5_cleaning/品牌/城市.json`、`.csv`、`.md` 和 `page_xx/card_xx.png`。
- cleaned 输出问题：看 `_cleaned_manifest.csv`。
- 质检问题：看 `_cleaned_quality_check.csv/md/json`。
- 需要重跑：看 `_rerun_required.csv/md`。
- 发布前门禁：看 `_delivery_gatekeeper_report.csv/md/json`。

## 新人不要做什么

- 不要在人工手机地址与脚本固定地址不一致时，直接判定“脚本漏 SKU”或“清洗误删”；朴朴会按具体收货地址/前置仓展示 SKU、价格、活动和库存。
- 不要在 `selected_address_text` 为空、`delivery_available` 非 true、`city_switch_verified` 非 true 时进入 cleaned、gatekeeper 或交付。
- 不要回到电脑微信小程序路线。
- 不要回到 Mac 模拟器路线。
- 不要继续 native-longshot 自动化，除非换手机后重新验证。
- 不要启用第三方截屏工具，除非用户重新授权。
- 不要把原始 H5 当成最终交付候选。
- 不要绕过 cleaned quality check。
- 不要 gatekeeper 未通过就生成交付包。
- 不要通过硬删卡片让 grade 好看。
- 不要隐藏 `possible_missing_sku`。
- 不要把标题不含品牌词作为主列表商品删除依据。
- 不要接口抓取，不要用大模型识别，不要新增截图模式。
